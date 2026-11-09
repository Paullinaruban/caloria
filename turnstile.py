"""Cloudflare Turnstile verification (stdlib HTTP only).

Env-gated: if TURNSTILE_SECRET is unset, verification is skipped (rate limiting
still applies) so local dev is frictionless. In production, set the site + secret
keys and every signup/login must carry a valid Turnstile token.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import config

_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def enabled() -> bool:
    return bool(config.TURNSTILE_SECRET)


def status_note() -> str:
    """One-line status for the boot log so Turnstile state is never silent."""
    if enabled():
        return "Turnstile ENABLED (bot challenge active on signup/login)."
    if config.REQUIRE_TURNSTILE:
        return ("Turnstile REQUIRED but NOT configured — signup/login will be "
                "REJECTED (fail-closed). Set TURNSTILE_SITE_KEY + TURNSTILE_SECRET.")
    return ("Turnstile DISABLED (no secret) — bot challenge is OFF; rate limits "
            "still apply. Set keys + REQUIRE_TURNSTILE=true in production.")


def verify(token: str, remote_ip: str | None = None) -> bool:
    """Return True if the Turnstile token is valid.

    When Turnstile is NOT configured: fail CLOSED (return False) if
    REQUIRE_TURNSTILE is set, otherwise fail open (return True) for local dev.
    This prevents a dropped secret from silently disabling bot protection."""
    if not enabled():
        return not config.REQUIRE_TURNSTILE
    if not token:
        return False
    params = {"secret": config.TURNSTILE_SECRET, "response": token}
    if remote_ip:
        params["remoteip"] = remote_ip
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(_VERIFY_URL, data=data, method="POST"), timeout=10
        ) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ok = bool(body.get("success"))
        if not ok:
            # DIAGNOSTIC ONLY — no behavior change (same bool returned). Surfaces
            # Cloudflare's reason in the Render logs so we can see WHY a token was
            # rejected. Common error-codes:
            #   invalid-input-secret   → secret wrong / for a different widget
            #   invalid-input-response → token invalid, or site-key & secret are from
            #                            different widgets, or the token has expired
            #   timeout-or-duplicate   → token already used, or older than ~300s
            #   missing-input-response → no token reached the backend
            # `hostname` is the domain Cloudflare recorded the token was solved on
            # (compare it to caloriaclub.com to catch a hostname restriction).
            print(f"[caloria] turnstile verify FAILED: error-codes="
                  f"{body.get('error-codes')} hostname={body.get('hostname')} "
                  f"challenge_ts={body.get('challenge_ts')}")
        return ok
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        # Fail-closed: if we can't verify, reject (don't let bots through on outage).
        # Logged (no behavior change) so an outage/egress block to Cloudflare — which
        # ALSO surfaces to users as "Bot check failed" — is distinguishable from a
        # real token rejection above.
        print(f"[caloria] turnstile verify ERROR (failing closed, could not reach Cloudflare): {e}")
        return False
