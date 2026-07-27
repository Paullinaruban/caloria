"""
revenuecat.py — Apple In-App Purchase (and future Google Play) entitlements,
via RevenueCat. Kept deliberately parallel to billing.py (Stripe).

Design
------
- The RevenueCat `app_user_id` is the Caloria `users.id` (as a string), so a
  purchase attaches to the SAME account the user is logged into. No duplicate
  accounts, works across the app + website.
- Access is still the single `plan == 'premium'` flag that the whole backend
  already checks (auth.is_premium). Apple purchases set it; Stripe purchases set
  it. Neither clobbers the other:
    * Stripe webhooks only touch rows matched by `stripe_customer`.
    * When an Apple sub lapses we downgrade to 'free' ONLY IF the account has no
      active Stripe subscription (see `_stripe_still_active`).
- Two activation paths, both standard:
    * POST /api/iap/sync  → server verifies with the RevenueCat REST API for an
      INSTANT unlock right after purchase (like Stripe confirm_checkout).
    * RevenueCat webhook   → durable source of truth (renewals, expirations,
      refunds, cross-device, Ask-to-Buy).

Only Python stdlib is used (urllib), matching the rest of the backend.
"""
import datetime
import hmac
import json
import time
import urllib.error
import urllib.request

import config
import db

RC_API_BASE = "https://api.revenuecat.com/v1"

# Webhook event types that GRANT access.
_GRANT = {
    "INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION",
    "NON_RENEWING_PURCHASE", "PRODUCT_CHANGE", "SUBSCRIPTION_EXTENDED",
    "TRANSFER", "TEMPORARY_ENTITLEMENT_GRANT",
}
# Event types that REVOKE access immediately.
_REVOKE = {"EXPIRATION", "REFUND", "SUBSCRIPTION_PAUSED"}
# Stripe statuses we treat as "still paying on the web" — used to avoid
# downgrading a web subscriber when their (separate) Apple sub lapses.
_STRIPE_ACTIVE = {"active", "trialing", "past_due", "manual"}


class RevenueCatError(Exception):
    pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_iso(ms) -> str:
    if not ms:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ms) / 1000.0))


def _parse_iso(s):
    """Parse a RevenueCat ISO timestamp → epoch ms (or None)."""
    if not s:
        return None
    s = s.strip().replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _get_user(user_id):
    with db.cursor() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _stripe_still_active(row) -> bool:
    """True if this account has an active Stripe subscription — so an Apple lapse
    must NOT downgrade them."""
    try:
        status = (row["subscription_status"] or "").lower()
        has_stripe = bool(row["stripe_subscription"] or row["stripe_customer"])
        return has_stripe and status in _STRIPE_ACTIVE
    except Exception:
        return False


def _store_to_provider(store) -> str:
    s = (store or "").upper()
    if s == "PLAY_STORE":
        return "play_store"
    if s == "APP_STORE":
        return "app_store"
    return (store or "app_store").lower()


# --------------------------------------------------------------------------- #
# the one write path — source-aware, idempotent
# --------------------------------------------------------------------------- #
def set_entitlement(user_id, *, active, provider="app_store", product=None,
                    expires_iso=None) -> bool:
    """Apply an IAP entitlement to a Caloria account. Returns the effective
    premium state after the update. Idempotent."""
    row = _get_user(user_id)
    if not row:
        print(f"[caloria][rc] no user for app_user_id={user_id!r} — ignoring")
        return False

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # NOTE: db.cursor()'s lock is NOT reentrant — we must not open a nested
    # cursor inside the block below. The Stripe fields we need for the lapse
    # decision aren't touched by this function, so the row fetched above is fine.
    stripe_active = _stripe_still_active(row)
    with db.cursor() as c:
        if active:
            # Grant premium via IAP. COALESCE keeps first-subscribe timestamp.
            c.execute(
                "UPDATE users SET plan='premium', iap_active=1, "
                "iap_provider=?, iap_product=COALESCE(?, iap_product), "
                "iap_expires_at=COALESCE(?, iap_expires_at), "
                "subscribed_at=COALESCE(subscribed_at, ?) WHERE id=?",
                (provider, product, expires_iso, now, user_id),
            )
            print(f"[caloria][rc] GRANT premium user={user_id} "
                  f"provider={provider} product={product} expires={expires_iso}")
            return True

        # Lapse: turn off the IAP flag. Only drop to 'free' if the account is not
        # an active Stripe (web) subscriber — never break a web customer.
        c.execute("UPDATE users SET iap_active=0, iap_expires_at=COALESCE(?, iap_expires_at) "
                  "WHERE id=?", (expires_iso, user_id))
        if stripe_active:
            print(f"[caloria][rc] IAP lapsed user={user_id} but Stripe active — keeping premium")
            return True
        c.execute("UPDATE users SET plan='free' WHERE id=? AND plan='premium'", (user_id,))
        print(f"[caloria][rc] REVOKE premium user={user_id} (no active Stripe)")
        return False


# --------------------------------------------------------------------------- #
# webhook (durable source of truth)
# --------------------------------------------------------------------------- #
def verify_webhook_auth(header_value: str) -> bool:
    """RevenueCat sends the exact Authorization header you configure in its
    dashboard. We compare it in constant time to our shared secret."""
    expected = config.REVENUECAT_WEBHOOK_AUTH
    if not expected:
        raise RevenueCatError("REVENUECAT_WEBHOOK_AUTH not set.")
    return hmac.compare_digest(str(header_value or ""), expected)


def handle_event(payload: dict) -> None:
    """Process one RevenueCat webhook payload: {"event": {...}, ...}."""
    event = (payload or {}).get("event") or {}
    etype = (event.get("type") or "").upper()

    # Ignore RevenueCat's connectivity TEST pings and anything without a user.
    if etype == "TEST":
        print("[caloria][rc] webhook TEST received — OK")
        return
    app_user_id = event.get("app_user_id") or event.get("original_app_user_id")
    try:
        user_id = int(str(app_user_id))
    except (TypeError, ValueError):
        print(f"[caloria][rc] webhook app_user_id={app_user_id!r} is not a Caloria id — ignoring")
        return

    # Only react to OUR entitlement (if the event names entitlements at all).
    ent = config.REVENUECAT_ENTITLEMENT
    ent_ids = event.get("entitlement_ids")
    if ent_ids and ent not in ent_ids:
        # A different product/entitlement — not our premium gate.
        return

    exp_ms = event.get("expiration_at_ms")
    if etype in _REVOKE:
        active = False
    elif etype in _GRANT:
        active = True
    elif exp_ms is not None:
        # CANCELLATION (auto-renew off), BILLING_ISSUE, etc.: access continues
        # until expiration.
        active = int(exp_ms) > _now_ms()
    else:
        print(f"[caloria][rc] webhook type={etype} — no actionable state, skipping")
        return

    set_entitlement(
        user_id,
        active=active,
        provider=_store_to_provider(event.get("store")),
        product=event.get("product_id"),
        expires_iso=_ms_to_iso(exp_ms),
    )


# --------------------------------------------------------------------------- #
# REST verify (instant unlock right after purchase)
# --------------------------------------------------------------------------- #
def _rc_get(path: str) -> dict:
    if not config.REVENUECAT_SECRET_KEY:
        raise RevenueCatError("REVENUECAT_SECRET_KEY not set.")
    req = urllib.request.Request(
        RC_API_BASE + path,
        headers={
            "Authorization": "Bearer " + config.REVENUECAT_SECRET_KEY,
            "Content-Type": "application/json",
            "X-Platform": "ios",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RevenueCatError(f"RevenueCat REST {e.code}: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        raise RevenueCatError(f"RevenueCat REST unreachable: {e}")


def sync_subscriber(user) -> bool:
    """Verify the user's entitlements directly with RevenueCat and apply them.
    Returns True if the account is (now) premium via IAP. Never raises to the
    caller path — logs and returns current state on failure."""
    ent = config.REVENUECAT_ENTITLEMENT
    try:
        data = _rc_get(f"/subscribers/{user['id']}")
    except RevenueCatError as e:
        print(f"[caloria][rc] sync lookup failed for user {user['id']}: {e}")
        return bool(user["iap_active"])

    subscriber = (data or {}).get("subscriber") or {}
    entitlements = subscriber.get("entitlements") or {}
    e = entitlements.get(ent)
    if not e:
        # No such entitlement for this user — treat as lapsed (Stripe-safe).
        return set_entitlement(user["id"], active=False)

    exp_ms = _parse_iso(e.get("expires_date"))          # None => lifetime/non-expiring
    grace_ms = _parse_iso(e.get("grace_period_expires_date"))
    now = _now_ms()
    active = (exp_ms is None) or (exp_ms > now) or (grace_ms is not None and grace_ms > now)

    # Figure out the store for bookkeeping.
    product_id = e.get("product_identifier")
    provider = "app_store"
    subs = subscriber.get("subscriptions") or {}
    if product_id and product_id in subs:
        provider = _store_to_provider(subs[product_id].get("store"))

    return set_entitlement(
        user["id"], active=active, provider=provider,
        product=product_id, expires_iso=_ms_to_iso(exp_ms),
    )
