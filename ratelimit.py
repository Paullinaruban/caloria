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
# action -> (cooldown_seconds, hourly_max)
EMAIL_GATES = {
    "verify": (60, 5),   # verification-code emails (signup resend / login / change-email)
    "reset":  (60, 5),   # password-reset-code emails
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
