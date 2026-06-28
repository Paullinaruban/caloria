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
