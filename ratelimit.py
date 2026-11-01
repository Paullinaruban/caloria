"""In-process per-key sliding-window rate limiter (stdlib only).

Protects auth & abuse-sensitive endpoints (signup, login, password-reset, verify
resend) from bursts, bots, and brute-force / credential-stuffing. Keys are
typically "<bucket>:<ip>" or "<bucket>:<email>".

Scope/limitation: state is in memory, so limits are per server process. The app
runs as a single ThreadingHTTPServer process, so this is correct here. Behind
multiple worker processes you'd move this to Redis — noted in the launch report.
"""
from __future__ import annotations

import threading
import time

import config

_lock = threading.Lock()
_hits: dict[str, list] = {}

# bucket -> (max_events, window_seconds)
LIMITS = {
    "signup": (5, 3600),      # 5 new accounts/hour/IP
    "login": (10, 900),       # 10 attempts / 15 min / IP
    "login_email": (5, 900),  # 5 attempts / 15 min / account (credential stuffing)
    "forgot": (4, 3600),      # 4 reset requests/hour/IP
    "resend": (4, 3600),      # 4 verification resends/hour/IP
    "verify_code": (15, 900), # 15 code attempts / 15 min / IP (brute-force guard)
    # Shared per-IP cap across ALL verification-related endpoints (signup / resend
    # / forgot / change-email), in a short and an hourly window. Keyed per IP, so
    # an attacker can't round-robin endpoints to multiply the per-bucket budgets.
    "email_req_ip_short": (config.EMAIL_REQ_PER_IP_SHORT, config.EMAIL_REQ_PER_IP_SHORT_WIN),
    "email_req_ip_hour": (config.EMAIL_REQ_PER_IP_HOUR, 3600),
    # Native-app verification start (also protected by the X-Caloria-App secret).
    "app_verify": (config.APP_VERIFY_PER_IP_HOUR, 3600),
    # Caloria Club: launch traffic arrives from TikTok/Instagram in-app browsers,
    # where whole mobile carriers share a handful of NAT'd IPs — per-IP limits
    # must leave real headroom. Duplicate emails are deduped regardless.
    "club_join": (30, 3600),     # 30 joins/hour/IP
    "club_answers": (120, 3600), # 3 taps per join × the same shared-IP crowd
}


def check(bucket: str, ident: str) -> tuple[bool, int]:
    """Record one event for (bucket, ident). Return (allowed, retry_after_seconds).

    allowed=False means the caller is over the limit and should be rejected.
    """
    cfg = LIMITS.get(bucket)
    if not cfg:
        return True, 0
    max_events, window = cfg
    key = f"{bucket}:{ident}"
    now = time.time()
    with _lock:
        q = _hits.get(key)
        if q is None:
            q = []
            _hits[key] = q
        # Drop events outside the window.
        cutoff = now - window
        while q and q[0] < cutoff:
            q.pop(0)
        if len(q) >= max_events:
            retry = int(q[0] + window - now) + 1
            return False, max(retry, 1)
        q.append(now)
        # Opportunistic cleanup to bound memory.
        if len(_hits) > 5000:
            _gc(now)
        return True, 0


def _gc(now: float) -> None:
    longest = max(w for _, w in LIMITS.values())
    dead = [k for k, q in _hits.items() if not q or q[-1] < now - longest]
    for k in dead:
        _hits.pop(k, None)


# --------------------------------------------------------------------------- #
# per-recipient email gate — cooldown + hourly cap (anti-spam for Resend)
# --------------------------------------------------------------------------- #
# The per-IP LIMITS above don't stop the same RECIPIENT from being emailed over
# and over (a bot rotating IPs, or shared-NAT traffic). This gate throttles by
# the target email address instead: at most one send per `cooldown` seconds and
# `hourly_max` sends per hour. It is keyed on the SUBMITTED email string, checked
# before any account lookup, so it behaves identically whether or not the account
# exists — it leaks no account-enumeration signal.
#
# action -> (cooldown_seconds, hourly_max). Values come from config (env-tunable).
EMAIL_GATES = {
    "verify": (config.VERIFY_EMAIL_COOLDOWN_SEC, config.VERIFY_EMAIL_HOURLY_MAX),
    "reset":  (config.RESET_EMAIL_COOLDOWN_SEC, config.RESET_EMAIL_HOURLY_MAX),
}
_EMAIL_GATE_WINDOW = 3600  # the hourly cap's window; also the gc horizon below


def email_gate(action: str, email: str) -> tuple[bool, int]:
    """Throttle code emails per recipient. Returns (allowed, retry_after_seconds)
    and records the send when allowed. Unknown actions are never limited."""
    cfg = EMAIL_GATES.get(action)
    if not cfg:
        return True, 0
    cooldown, hourly_max = cfg
    key = f"emailgate:{action}:{(email or '').strip().lower()}"
    now = time.time()
    with _lock:
        q = _hits.get(key)
        if q is None:
            q = []
            _hits[key] = q
        cutoff = now - _EMAIL_GATE_WINDOW
        while q and q[0] < cutoff:
            q.pop(0)
        if len(q) >= hourly_max:                       # hourly cap hit
            return False, max(int(q[0] + _EMAIL_GATE_WINDOW - now) + 1, 1)
        if q and (now - q[-1]) < cooldown:             # still within cooldown
            return False, max(int(cooldown - (now - q[-1])) + 1, 1)
        q.append(now)
        if len(_hits) > 5000:
            _gc(now)
        return True, 0


# --------------------------------------------------------------------------- #
# GLOBAL auth-email circuit breaker (kill switch + hourly/daily budget)
# --------------------------------------------------------------------------- #
# Vector-independent backstop: the per-IP and per-recipient gates above throttle
# one source or one address, but cannot see TOTAL outbound volume. An attacker
# rotating IPs and inventing fresh addresses would slip past both. This caps the
# whole service's verification/reset email output and can be cut to zero instantly
# with AUTH_EMAIL_KILL_SWITCH. All thresholds are env-configurable.
_GLOBAL_HOUR = "authemail:global:hour"
_GLOBAL_DAY = "authemail:global:day"
_last_alert = [0.0]  # throttle the alert log so it doesn't spam under sustained abuse


def auth_email_budget_ok(action: str = "auth") -> bool:
    """True if one more auth-code email may be sent NOW. Records the send when it
    returns True. When False, the caller must NOT call Resend. Logs a throttled
    [email-guard] ALERT so unusual volume / an active kill switch is visible."""
    if config.AUTH_EMAIL_KILL_SWITCH:
        _alert(f"KILL SWITCH active — blocked {action} email")
        return False
    hourly_max = config.AUTH_EMAIL_HOURLY_MAX
    daily_max = config.AUTH_EMAIL_DAILY_MAX
    now = time.time()
    with _lock:
        # Check BOTH windows before recording, so a block in one doesn't consume
        # budget in the other.
        hour = _count_locked(_GLOBAL_HOUR, now, 3600)
        day = _count_locked(_GLOBAL_DAY, now, 86400)
        over = ""
        if hourly_max > 0 and hour >= hourly_max:
            over = "hourly"
        elif daily_max > 0 and day >= daily_max:
            over = "daily"
        if over:
            _alert(f"{over} global budget reached — blocked {action} email "
                   f"(hour={hour}/{hourly_max}, day={day}/{daily_max})")
            return False
        _hits.setdefault(_GLOBAL_HOUR, []).append(now)
        _hits.setdefault(_GLOBAL_DAY, []).append(now)
        return True


def _count_locked(key: str, now: float, window: int) -> int:
    """Trim `key` to `window` and return the live count. Caller holds _lock."""
    q = _hits.get(key)
    if not q:
        return 0
    cutoff = now - window
    while q and q[0] < cutoff:
        q.pop(0)
    return len(q)


def _alert(msg: str) -> None:
    now = time.time()
    if now - _last_alert[0] >= 30:   # at most one alert line every 30s
        _last_alert[0] = now
        print(f"[email-guard] ALERT: {msg}", flush=True)


def auth_email_counts() -> dict:
    """Current global auth-email counters (for health/monitoring)."""
    now = time.time()
    with _lock:
        return {
            "hour": _count_locked(_GLOBAL_HOUR, now, 3600),
            "hour_max": config.AUTH_EMAIL_HOURLY_MAX,
            "day": _count_locked(_GLOBAL_DAY, now, 86400),
            "day_max": config.AUTH_EMAIL_DAILY_MAX,
            "kill_switch": config.AUTH_EMAIL_KILL_SWITCH,
        }
