"""Secure, single-use, time-limited account tokens (email verify + password reset).

Security model:
  * The raw token is a 256-bit URL-safe secret, returned ONCE to the caller to
    embed in a link. Only its SHA-256 hash is stored — a DB leak cannot be used
    to forge links.
  * Tokens expire (verify: hours, reset: ~1h) and are single-use (used_at).
  * Issuing a new token of a kind invalidates the user's older unused ones, so a
    resend can't leave multiple valid links floating around.
  * Verification of a presented token is constant-time on the hash lookup and
    rejects expired/used/forged tokens uniformly.
"""
from __future__ import annotations

import datetime
import hashlib
import secrets

import db


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def issue(user_id: int, kind: str, ttl_hours: int) -> str:
    """Create a token of `kind` for the user; return the RAW token (store nowhere)."""
    raw = secrets.token_urlsafe(32)
    expires = (_now() + datetime.timedelta(hours=ttl_hours)).isoformat()
    with db.cursor() as c:
        # Invalidate older unused tokens of the same kind.
        c.execute(
            "DELETE FROM account_tokens WHERE user_id = ? AND kind = ? AND used_at IS NULL",
            (user_id, kind),
        )
        c.execute(
            "INSERT INTO account_tokens (user_id, kind, token_hash, expires_at) "
            "VALUES (?,?,?,?)",
            (user_id, kind, _hash(raw), expires),
        )
    return raw


def consume(raw: str, kind: str) -> int | None:
    """Validate & burn a token. Returns the user_id on success, else None.

    Fails (None) for unknown, wrong-kind, expired, or already-used tokens.
    """
    if not raw:
        return None
    h = _hash(raw)
    with db.cursor() as c:
        row = c.execute(
            "SELECT id, user_id, expires_at, used_at FROM account_tokens "
            "WHERE token_hash = ? AND kind = ?",
            (h, kind),
        ).fetchone()
        if not row or row["used_at"]:
            return None
        try:
            expires = datetime.datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return None
        if _now() > expires:
            return None
        c.execute(
            "UPDATE account_tokens SET used_at = ? WHERE id = ?",
            (_now().isoformat(), row["id"]),
        )
        return row["user_id"]


def issue_code(user_id: int, kind: str, ttl_minutes: int) -> str:
    """Create a 6-digit numeric code for the user; return the RAW code.

    Stored hashed (never in plaintext). Issuing a new code invalidates the
    user's older unused codes of the same kind.
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires = (_now() + datetime.timedelta(minutes=ttl_minutes)).isoformat()
    with db.cursor() as c:
        c.execute(
            "DELETE FROM account_tokens WHERE user_id = ? AND kind = ? AND used_at IS NULL",
            (user_id, kind),
        )
        c.execute(
            "INSERT INTO account_tokens (user_id, kind, token_hash, expires_at) "
            "VALUES (?,?,?,?)",
            (user_id, kind, _hash(code), expires),
        )
    return code


def verify_code(user_id: int, kind: str, code: str, max_attempts: int) -> bool:
    """Validate a 6-digit code for the user. Returns True on success.

    Constant-time compare; enforces expiry, single-use, and an attempt cap
    (after which the code is burned and the user must request a new one).
    """
    code = (code or "").strip()
    if not (code.isdigit() and len(code) == 6):
        return False
    with db.cursor() as c:
        row = c.execute(
            "SELECT id, token_hash, expires_at, used_at, attempts FROM account_tokens "
            "WHERE user_id = ? AND kind = ? AND used_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (user_id, kind),
        ).fetchone()
        if not row:
            return False
        try:
            expires = datetime.datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return False
        if _now() > expires:
            return False
        import hmac
        ok = hmac.compare_digest(
            row["token_hash"],
            _hash(code),
        )
        if ok:
            c.execute("UPDATE account_tokens SET used_at = ? WHERE id = ?",
                      (_now().isoformat(), row["id"]))
            return True
        # Wrong code: count the attempt; burn the code once the cap is reached.
        attempts = (row["attempts"] or 0) + 1
        if attempts >= max_attempts:
            c.execute("UPDATE account_tokens SET used_at = ? WHERE id = ?",
                      (_now().isoformat(), row["id"]))
        else:
            c.execute("UPDATE account_tokens SET attempts = ? WHERE id = ?",
                      (attempts, row["id"]))
        return False


def purge_expired() -> None:
    """Housekeeping: drop expired/used tokens. Safe to call periodically."""
    cutoff = (_now() - datetime.timedelta(days=2)).isoformat()
    with db.cursor() as c:
        c.execute(
            "DELETE FROM account_tokens WHERE used_at IS NOT NULL OR expires_at < ?",
            (cutoff,),
        )
