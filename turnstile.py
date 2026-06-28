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


def verify(token: str, remote_ip: str | None = None) -> bool:
    """Return True if the Turnstile token is valid (or if Turnstile is disabled)."""
    if not enabled():
        return True
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
        return bool(body.get("success"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        # Fail-closed: if we can't verify, reject (don't let bots through on outage).
        return False
