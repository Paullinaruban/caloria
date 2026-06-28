"""Accounts, password hashing, sessions, and profile/targets.

Passwords use PBKDF2-HMAC-SHA256 (stdlib). Sessions are random bearer tokens
stored in SQLite. Nutrition targets are computed with the Mifflin-St Jeor
equation (gender-aware) + activity multiplier + goal adjustment.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import secrets

import config
import db
import email_send
import tokens

_PBKDF_ROUNDS = 200_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------- password hashing ----------
def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF_ROUNDS
    ).hex()


def _verify(password: str, salt: str, expected: str) -> bool:
    return secrets.compare_digest(_hash(password, salt), expected)


# ---------- account lifecycle ----------
def signup(email: str, password: str, name: str = "") -> dict:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise AuthError("Please enter a valid email address.")
    if len(password or "") < 6:
        raise AuthError("Password must be at least 6 characters.")
    salt = secrets.token_hex(16)
    pw_hash = _hash(password, salt)
    accepted_at = datetime.datetime.utcnow().isoformat() + "Z"
    try:
        with db.cursor() as c:
            c.execute(
                "INSERT INTO users (email, name, pw_salt, pw_hash, terms_accepted, "
                "privacy_accepted, policy_version, policy_accepted_at) "
                "VALUES (?,?,?,?,1,1,?,?)",
                (email, (name or "").strip()[:60], salt, pw_hash,
                 config.POLICY_VERSION, accepted_at),
            )
            user_id = c.lastrowid
    except Exception as e:  # UNIQUE constraint
        if "UNIQUE" in str(e):
            raise AuthError("An account with that email already exists.", 409)
        raise
    send_verification(user_id, email)  # best-effort; never blocks signup
    return {"token": _new_session(user_id), "user": _public_user(_get_user(user_id))}


# ---------- email verification (6-digit code) ----------
def send_verification(user_id: int, email: str) -> None:
    """Issue a fresh 6-digit verification code and email it. Never raises."""
    try:
        code = tokens.issue_code(user_id, "verify", config.VERIFY_CODE_TTL_MINUTES)
        email_send.send_verification_code(email, code)
    except Exception as e:  # noqa: BLE001 — email problems must not break the flow
        print(f"[caloria] verification email failed for {email}: {e}")


def verify_email_code(email: str, code: str) -> bool:
    """Validate a user's 6-digit code and mark the email verified on success."""
    email = (email or "").strip().lower()
    row = _get_user_by_email(email)
    if not row:
        return False
    if row["email_verified"]:
        return True  # already verified — treat as success (idempotent)
    if not tokens.verify_code(row["id"], "verify", code, config.VERIFY_CODE_MAX_ATTEMPTS):
        return False
    with db.cursor() as c:
        c.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (row["id"],))
    return True


def resend_verification(email: str) -> None:
    """Resend a verification code for an unverified account. Neutral (no enumeration)."""
    email = (email or "").strip().lower()
    row = _get_user_by_email(email)
    if row and not row["email_verified"]:
        send_verification(row["id"], email)


# ---------- password reset ----------
def request_reset(email: str) -> None:
    """Email a reset link if the account exists. Always silent (no enumeration)."""
    email = (email or "").strip().lower()
    row = _get_user_by_email(email)
    if not row:
        return
    try:
        raw = tokens.issue(row["id"], "reset", config.RESET_TOKEN_TTL_HOURS)
        link = f"{config.APP_BASE_URL}/?reset={raw}"
        email_send.send_reset(email, link)
    except Exception as e:  # noqa: BLE001
        print(f"[caloria] reset email failed for {email}: {e}")


def reset_password(raw_token: str, new_password: str) -> bool:
    if len(new_password or "") < 6:
        raise AuthError("Password must be at least 6 characters.")
    user_id = tokens.consume(raw_token, "reset")
    if not user_id:
        return False
    salt = secrets.token_hex(16)
    pw_hash = _hash(new_password, salt)
    with db.cursor() as c:
        c.execute(
            "UPDATE users SET pw_salt = ?, pw_hash = ? WHERE id = ?", (salt, pw_hash, user_id)
        )
        # Reset invalidates all existing sessions (force re-login everywhere).
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return True


def _get_user_by_email(email: str):
    with db.cursor() as c:
        return c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def login(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    with db.cursor() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not _verify(password or "", row["pw_salt"], row["pw_hash"]):
        raise AuthError("Incorrect email or password.", 401)
    if not _is_active(row):
        raise AuthError("This account has been deactivated. Please contact support.", 403)
    return {"token": _new_session(row["id"]), "user": _public_user(row)}


def _is_active(row) -> bool:
    # Older rows created before the column existed default to active.
    try:
        return bool(row["active"])
    except (IndexError, KeyError):
        return True


def _new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db.cursor() as c:
        c.execute("INSERT INTO sessions (token, user_id) VALUES (?,?)", (token, user_id))
    return token


def logout(token: str) -> None:
    with db.cursor() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---------- lookups ----------
def _get_user(user_id: int):
    with db.cursor() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def user_for_token(token: str | None):
    """Return the user row for a valid, unexpired bearer token, or None."""
    if not token:
        return None
    with db.cursor() as c:
        row = c.execute(
            "SELECT u.*, s.created_at AS _sess_created "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if _session_expired(row["_sess_created"]):
            c.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
    if not _is_active(row):  # deactivated accounts can't use existing sessions
        return None
    return row


def _session_expired(created_at: str | None) -> bool:
    if not created_at:
        return False
    try:
        created = datetime.datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    return datetime.datetime.utcnow() - created > datetime.timedelta(days=config.SESSION_TTL_DAYS)


def is_verified(row) -> bool:
    """Effective verification status (admins / dev mode count as verified)."""
    if config.DEV_UNLIMITED:
        return True
    if (row["email"] or "").lower() in config.ADMIN_EMAILS:
        return True
    return bool(row["email_verified"])


def is_premium(row) -> bool:
    """Effective premium status — includes the developer/admin testing overrides."""
    import config
    if config.DEV_UNLIMITED:
        return True
    email = (row["email"] or "").lower()
    if email in config.ADMIN_EMAILS:
        return True
    return row["plan"] == "premium"


def _public_user(row) -> dict:
    import config
    premium = is_premium(row)
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"] or "",
        # Report the EFFECTIVE plan so the UI unlocks everything in dev/admin mode.
        "plan": "premium" if premium else row["plan"],
        "is_admin": (row["email"] or "").lower() in config.ADMIN_EMAILS,
        "dev_unlimited": config.DEV_UNLIMITED,
        "scans_used": row["scans_used"],
        # The user's own verification state (not an internal usage counter).
        "email_verified": is_verified(row),
        "needs_verification": config.REQUIRE_EMAIL_VERIFICATION and not is_verified(row),
        "profile": json.loads(row["profile_json"]) if row["profile_json"] else None,
        "targets": json.loads(row["targets_json"]) if row["targets_json"] else None,
    }


def public_user(row) -> dict:
    return _public_user(row)


# ---------- profile + targets ----------
# Nutrition math lives in the single source of truth: nutrition_engine.py.
import nutrition_engine


def compute_targets(profile: dict) -> dict:
    return nutrition_engine.compute_targets(profile)


def save_profile(user_id: int, profile: dict) -> dict:
    targets = compute_targets(profile)
    with db.cursor() as c:
        c.execute(
            "UPDATE users SET profile_json = ?, targets_json = ? WHERE id = ?",
            (json.dumps(profile), json.dumps(targets), user_id),
        )
    return {"user": _public_user(_get_user(user_id))}
