"""
appauth.py — NATIVE iOS onboarding authentication (verify-first flow).

The native iOS app collects, in this order:
    questionnaire → name → email → EMAIL VERIFICATION → password → Apple IAP → account
so the email is verified BEFORE any account exists. This module holds a short
pending record per email (name + hashed code) and creates the real account only
at the final register step. The WEBSITE's own auth flow (auth.py / Turnstile) is
completely untouched — these are separate, additive endpoints.

Endpoints (wired in server.py; app-gated by X-Caloria-App when APP_CLIENT_SECRET set):
    POST /api/app/verify/start  {email, name?}                → email a 6-digit code
    POST /api/app/verify/check  {email, code}                 → mark the email verified
    POST /api/app/register      {email, password, name, profile} → create the verified account
    POST /api/app/iap/grant     (auth)                        → mark premium after a purchase
"""
import datetime
import hashlib
import hmac
import secrets

import auth
import config
import db
import email_send

CODE_TTL_MIN = 15
MAX_ATTEMPTS = 6


class AppAuthError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


def _init():
    with db.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS app_pending_verifications (
            email       TEXT PRIMARY KEY,
            name        TEXT,
            code_hash   TEXT,
            expires_at  TEXT,
            verified    INTEGER NOT NULL DEFAULT 0,
            attempts    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )""")


def _now():
    return datetime.datetime.utcnow().replace(microsecond=0)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_code(email, code):
    return hashlib.sha256(f"{email.lower()}:{code}".encode()).hexdigest()


def app_secret_ok(header_value) -> bool:
    """Gate the app endpoints. When APP_CLIENT_SECRET is set the app must send it
    in X-Caloria-App; otherwise (dev) the endpoints are open."""
    want = config.APP_CLIENT_SECRET
    if not want:
        return True
    return hmac.compare_digest(str(header_value or ""), want)


def _account_exists(email):
    with db.cursor() as c:
        return c.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None


# --------------------------------------------------------------------------- #
# 1) start verification — email a code, create NO account
# --------------------------------------------------------------------------- #
def start_verification(email, name=""):
    email = (email or "").strip().lower()
    if not auth._EMAIL_RE.match(email):
        raise AppAuthError("Please enter a valid email address.")
    if _account_exists(email):
        raise AppAuthError("An account with that email already exists. Please log in.", 409)
    code = f"{secrets.randbelow(1000000):06d}"
    expires = _iso(_now() + datetime.timedelta(minutes=CODE_TTL_MIN))
    _init()
    with db.cursor() as c:
        c.execute(
            "INSERT INTO app_pending_verifications (email, name, code_hash, expires_at, verified, attempts) "
            "VALUES (?,?,?,?,0,0) ON CONFLICT(email) DO UPDATE SET "
            "name=excluded.name, code_hash=excluded.code_hash, expires_at=excluded.expires_at, verified=0, attempts=0",
            (email, (name or "").strip()[:60], _hash_code(email, code), expires),
        )
    sent = True
    try:
        email_send.send_verification_code(email, code, trace="app-signup")
    except Exception as e:  # noqa: BLE001 — never break the flow on a mail hiccup
        sent = False
        print(f"[caloria][appauth] verification email failed for {email}: {e}")
    return {"ok": True, "email": email, "verification_email_sent": sent}


# --------------------------------------------------------------------------- #
# 2) check the code — mark the email verified (still no account)
# --------------------------------------------------------------------------- #
def check_code(email, code):
    email = (email or "").strip().lower()
    code = (code or "").strip()
    _init()
    # Read in its own block. IMPORTANT: db.cursor() only commits when the block
    # exits WITHOUT an exception, so any write that is followed by a raise must
    # live in its own committed block — otherwise the write is rolled back (this
    # is exactly what broke the attempt counter / brute-force lockout before).
    with db.cursor() as c:
        row = c.execute("SELECT * FROM app_pending_verifications WHERE email = ?", (email,)).fetchone()
    if not row:
        raise AppAuthError("Please request a verification code first.")
    if row["attempts"] >= MAX_ATTEMPTS:
        raise AppAuthError("Too many attempts. Please request a new code.", 429)
    try:
        exp = datetime.datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        exp = _now() - datetime.timedelta(seconds=1)
    if _now() > exp:
        raise AppAuthError("That code has expired. Please request a new one.")
    if not hmac.compare_digest(row["code_hash"], _hash_code(email, code)):
        with db.cursor() as c:                     # committed increment, THEN raise
            c.execute("UPDATE app_pending_verifications SET attempts = attempts + 1 WHERE email = ?", (email,))
        raise AppAuthError("That code is incorrect.")
    with db.cursor() as c:
        c.execute("UPDATE app_pending_verifications SET verified = 1 WHERE email = ?", (email,))
    return {"ok": True, "verified": True}


# --------------------------------------------------------------------------- #
# 3) register — create the account (email already verified), save the profile
# --------------------------------------------------------------------------- #
def register(email, password, name="", profile=None):
    email = (email or "").strip().lower()
    if len(password or "") < 6:
        raise AppAuthError("Password must be at least 6 characters.")
    _init()
    with db.cursor() as c:
        row = c.execute("SELECT * FROM app_pending_verifications WHERE email = ?", (email,)).fetchone()
        if not row or not row["verified"]:
            raise AppAuthError("Please verify your email first.", 403)
        name = name or row["name"] or ""
    if _account_exists(email):
        raise AppAuthError("An account with that email already exists. Please log in.", 409)

    salt = secrets.token_hex(16)
    pw_hash = auth._hash(password, salt)
    accepted_at = _iso(_now())
    with db.cursor() as c:
        try:
            c.execute(
                "INSERT INTO users (email, name, pw_salt, pw_hash, email_verified, terms_accepted, "
                "privacy_accepted, policy_version, policy_accepted_at) VALUES (?,?,?,?,1,1,1,?,?)",
                (email, (name or "").strip()[:60], salt, pw_hash, config.POLICY_VERSION, accepted_at),
            )
            user_id = c.lastrowid
        except Exception as e:
            if "UNIQUE" in str(e):
                raise AppAuthError("An account with that email already exists. Please log in.", 409)
            raise
        c.execute("DELETE FROM app_pending_verifications WHERE email = ?", (email,))

    auth.grant_founding_if_invited(user_id, email)
    if profile:
        try:
            auth.save_profile(user_id, profile)  # same nutrition_engine math + targets
        except Exception as e:  # noqa: BLE001
            print(f"[caloria][appauth] save_profile failed for {user_id}: {e}")
    token = auth._new_session(user_id)
    return {"token": token, "user": auth._public_user(auth._get_user(user_id))}


# --------------------------------------------------------------------------- #
# 4) grant premium after an Apple purchase
# --------------------------------------------------------------------------- #
def grant_premium(user):
    """Mark the user premium after a StoreKit-verified purchase.

    PRODUCTION NOTE: for a hardened setup, back this with server-side Apple
    receipt validation, or (recommended) the RevenueCat webhook already wired in
    revenuecat.py — the native app calls Purchases.logIn(user.id) so the webhook
    attributes the purchase to this account. This endpoint is the simple path."""
    with db.cursor() as c:
        c.execute(
            "UPDATE users SET plan='premium', subscription_status='active', "
            "iap_active=1, iap_provider='app_store' WHERE id=?",
            (user["id"],),
        )
    return {"ok": True, "user": auth._public_user(auth._get_user(user["id"]))}
