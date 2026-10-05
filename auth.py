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

# OWASP (2024) recommends >= 600,000 iterations for PBKDF2-HMAC-SHA256. Hashes are
# stored self-describing ("pbkdf2_sha256$<rounds>$<hex>") so the iteration count
# travels with each hash and can be raised later without locking anyone out.
_PBKDF_ROUNDS = 600_000
_LEGACY_ROUNDS = 200_000   # bare-hex hashes written before the versioned format
_PBKDF_ALGO = "pbkdf2_sha256"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# ---------- password hashing ----------
def _pbkdf2(password: str, salt: str, rounds: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), rounds
    ).hex()


def _hash(password: str, salt: str, rounds: int = _PBKDF_ROUNDS) -> str:
    """Self-describing hash: 'pbkdf2_sha256$<rounds>$<hex>'."""
    return f"{_PBKDF_ALGO}${rounds}${_pbkdf2(password, salt, rounds)}"


def _parse_hash(stored: str):
    """Return (rounds, digest_hex, is_legacy) for a stored pw_hash value.
    Legacy rows are bare hex from before the versioned format existed."""
    if stored and "$" in stored:
        _algo, rounds, digest = stored.split("$", 2)
        return int(rounds), digest, False
    return _LEGACY_ROUNDS, (stored or ""), True


def _verify(password: str, salt: str, stored: str) -> bool:
    rounds, digest, _legacy = _parse_hash(stored)
    return secrets.compare_digest(_pbkdf2(password, salt, rounds), digest)


def _needs_rehash(stored: str) -> bool:
    rounds, _digest, is_legacy = _parse_hash(stored)
    return is_legacy or rounds < _PBKDF_ROUNDS


def _maybe_rehash(user_id: int, password: str, stored: str) -> None:
    """On a successful login with an outdated hash, transparently re-hash the
    password at current parameters (fresh salt). Never changes the password and
    never blocks login if the write fails."""
    if not _needs_rehash(stored):
        return
    try:
        salt = secrets.token_hex(16)
        with db.cursor() as c:
            c.execute(
                "UPDATE users SET pw_salt = ?, pw_hash = ? WHERE id = ?",
                (salt, _hash(password, salt), user_id),
            )
    except Exception as e:  # noqa: BLE001 — a rehash failure must not break login
        print(f"[caloria] password rehash failed for user {user_id}: {e}")


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
    grant_founding_if_invited(user_id, email)  # permanent badge for invited emails
    sent = send_verification(user_id, email, context="signup")  # in-request; FIRST email must be reliable
    return {
        "token": _new_session(user_id),
        "user": _public_user(_get_user(user_id)),
        # So the frontend can avoid a fake "check your email" message when the
        # email provider isn't configured or the send failed.
        "verification_required": bool(config.REQUIRE_EMAIL_VERIFICATION),
        "verification_email_sent": sent,
    }


# ---------- email verification (6-digit code) ----------
def send_verification(user_id: int, email: str, context: str = "verify") -> bool:
    """Issue a fresh 6-digit code and email it — SYNCHRONOUSLY, within the current
    request. Returns True only if the provider actually accepted the message.
    `context` ("signup"/"resend"/"login"/…) tags the step-by-step diagnostic log
    so the FIRST send and a later Resend can be compared line-by-line. Never raises."""
    import time
    t0 = time.time()
    print(f"[verify-trace] {context}: send_verification ENTER user={user_id} email={email}", flush=True)
    try:
        code = tokens.issue_code(user_id, "verify", config.VERIFY_CODE_TTL_MINUTES)
    except Exception as e:  # noqa: BLE001 — code issues must not break auth
        print(f"[verify-trace] {context}: issue_code FAILED {type(e).__name__}: {e}", flush=True)
        return False
    print(f"[verify-trace] {context}: code issued, email_ready={config.email_ready()}", flush=True)
    if not config.email_ready():
        print(f"[verify-trace] {context}: NOT sent — email provider not configured ({email})", flush=True)
        return False
    try:
        email_send.send_verification_code(
            email, code, timeout=config.EMAIL_TIMEOUT_INTERACTIVE, trace=context)
        print(f"[verify-trace] {context}: RESULT sent=True in {int((time.time()-t0)*1000)}ms", flush=True)
        return True
    except Exception as e:  # noqa: BLE001 — a failed send must not break signup/login
        print(f"[verify-trace] {context}: RESULT sent=False {type(e).__name__}: {e} "
              f"in {int((time.time()-t0)*1000)}ms", flush=True)
        return False


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
        send_verification(row["id"], email, context="resend")


def change_email(user_id: int, new_email: str) -> str:
    """Change a user's email (e.g. a typo at signup), reset verification, and send
    a fresh code to the new address. Returns the normalized new email. Raises
    AuthError on an invalid or already-taken address."""
    new_email = (new_email or "").strip().lower()
    if not _EMAIL_RE.match(new_email):
        raise AuthError("Please enter a valid email address.")
    with db.cursor() as c:
        cur = c.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        if cur and (cur["email"] or "").lower() == new_email:
            raise AuthError("That is already your email address.")
        taken = c.execute(
            "SELECT id FROM users WHERE email = ? AND id <> ?", (new_email, user_id)
        ).fetchone()
        if taken:
            raise AuthError("An account with that email already exists.", 409)
        # New address starts unverified — this is the whole point of the gate.
        c.execute("UPDATE users SET email = ?, email_verified = 0 WHERE id = ?",
                  (new_email, user_id))
    send_verification(user_id, new_email, context="change_email")
    return new_email


# ---------- password reset (6-digit code, same UX as email verification) ----------
def request_reset(email: str) -> None:
    """Email a 6-digit reset code if the account exists. Always silent (no enumeration)."""
    email = (email or "").strip().lower()
    row = _get_user_by_email(email)
    if not row:
        return
    try:
        code = tokens.issue_code(row["id"], "reset", config.RESET_CODE_TTL_MINUTES)
        email_send.send_reset_code(email, code)
    except Exception as e:  # noqa: BLE001
        print(f"[caloria] reset email failed for {email}: {e}")


def reset_password_with_code(email: str, code: str, new_password: str) -> bool:
    """Verify the 6-digit reset code and set a new password. Returns False for a
    bad/expired code or unknown account (neutral — no enumeration)."""
    if len(new_password or "") < 6:
        raise AuthError("Password must be at least 6 characters.")
    email = (email or "").strip().lower()
    row = _get_user_by_email(email)
    if not row:
        return False
    if not tokens.verify_code(row["id"], "reset", code, config.RESET_CODE_MAX_ATTEMPTS):
        return False
    salt = secrets.token_hex(16)
    pw_hash = _hash(new_password, salt)
    with db.cursor() as c:
        c.execute(
            "UPDATE users SET pw_salt = ?, pw_hash = ? WHERE id = ?", (salt, pw_hash, row["id"])
        )
        # Reset invalidates all existing sessions (force re-login everywhere).
        c.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
    print(f"[caloria] password reset OK for user={row['id']} email={email}")  # observability
    # Auto-login: the emailed one-time code already proved control of this
    # account, so we issue a fresh session immediately. This removes the
    # error-prone "now re-type your new password on the login screen" step,
    # where a password manager / iCloud Keychain often autofills the OLD saved
    # password and causes a false "Incorrect email or password". Returns the
    # same {token, user} shape as login().
    fresh = _get_user(row["id"])
    return {"token": _new_session(row["id"]), "user": _public_user(fresh)}


def check_reset_code(email: str, code: str) -> bool:
    """Non-burning validity check so the UI can advance from the code screen to
    the new-password screen before consuming the code."""
    email = (email or "").strip().lower()
    row = _get_user_by_email(email)
    if not row:
        return False
    return tokens.check_code(row["id"], "reset", code)


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
    _maybe_rehash(row["id"], password or "", row["pw_hash"])
    # Invited members who created their account before being added to the list
    # earn the badge on their next login (while the window is open).
    if grant_founding_if_invited(row["id"], email):
        row = _get_user(row["id"])
    # If this account still needs to verify its email, send a fresh code NOW —
    # the login itself is what surfaces the verification modal, so the very first
    # request must deliver a code (previously none was sent until "Resend").
    if config.REQUIRE_EMAIL_VERIFICATION and not is_verified(row):
        send_verification(row["id"], email, context="login")
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


# Stripe subscription statuses that mean the customer is entitled: paying,
# trialing, in the automatic-retry grace window, or an admin-granted comp.
_ENTITLED_STRIPE_STATUS = {"active", "trialing", "past_due", "manual"}


def _row_get(row, key, default=None):
    """Safe access for an sqlite3.Row (missing column -> default)."""
    try:
        v = row[key]
        return default if v is None else v
    except (KeyError, IndexError):
        return default


def _has_live_entitlement(row) -> bool:
    """True if the account holds a live paid entitlement from EITHER billing
    source, independent of the cached `plan` flag.

    This is the resilience guarantee: a paying subscriber is NEVER shown the
    paywall just because the `plan` column was flipped to 'free' by a transient
    or cross-source (Stripe <-> Apple/Google) webhook race.
    """
    # Active Stripe subscription (web).
    has_stripe = bool(_row_get(row, "stripe_subscription") or _row_get(row, "stripe_customer"))
    status = str(_row_get(row, "subscription_status", "") or "").lower()
    if has_stripe and status in _ENTITLED_STRIPE_STATUS:
        return True
    # Active Apple / Google in-app purchase.
    if _row_get(row, "iap_active"):
        return True
    return False


def is_premium(row) -> bool:
    """Effective premium status. The cached `plan` flag is the fast path; a live
    Stripe/IAP entitlement is honored even if that flag was incorrectly flipped,
    so an existing paying customer can never be locked out of what they paid for."""
    import config
    if config.DEV_UNLIMITED:
        return True
    email = (row["email"] or "").lower()
    if email in config.ADMIN_EMAILS:
        return True
    if row["plan"] == "premium":
        return True
    return _has_live_entitlement(row)


def grant_founding_if_invited(user_id: int, email: str) -> bool:
    """Permanently grant the Founding Member badge to EVERYONE who joins while
    the launch window is open (before FOUNDING_MEMBER_DEADLINE), plus anyone on
    the explicit invite list. The badge is set exactly once and is never revoked.
    Once the window closes, only invite-listed emails can still earn it — normal
    future signups never do. Idempotent, safe on every signup/login, never raises."""
    try:
        email = (email or "").strip().lower()
        # Open window ⇒ every new member is a Founding Member. Closed window ⇒
        # only an explicitly invited email still qualifies (list is empty by default).
        eligible = config.founding_window_open() or email in config.FOUNDING_MEMBER_EMAILS
        if not eligible:
            return False
        now = datetime.datetime.utcnow().isoformat() + "Z"
        with db.cursor() as c:
            # Only set once — the badge is permanent and its grant date is kept.
            cur = c.execute(
                "UPDATE users SET founding_member = 1, founding_member_at = ? "
                "WHERE id = ? AND founding_member = 0",
                (now, user_id),
            )
            return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001 — badge must never break auth
        print(f"[caloria] founding badge grant failed for {email}: {e}")
        return False


def is_founding(row) -> bool:
    try:
        return bool(row["founding_member"])
    except (IndexError, KeyError):
        return False


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
        "founding_member": is_founding(row),
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
