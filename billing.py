"""Stripe subscriptions (stdlib only).

Creates Checkout Sessions for the Monthly and Yearly plans (amounts come from
config.MONTHLY_PRICE_USD / YEARLY_PRICE_USD — the single pricing source), and
verifies webhooks to upgrade/downgrade accounts. Prices can be supplied via env
(STRIPE_PRICE_MONTHLY / STRIPE_PRICE_YEARLY) or auto-created on first use and
cached in the kv table.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import db
import email_send

_API = "https://api.stripe.com/v1"

# Window during which a freshly-created Checkout Session is reused instead of
# creating another one for the same user+interval. Collapses double-clicks,
# network retries and back-button re-submits into a single session so we never
# spin up duplicate Checkout Sessions / PaymentIntents for one payment attempt.
_CHECKOUT_REUSE_SECONDS = 30 * 60


class BillingError(RuntimeError):
    pass


def _stripe(path: str, params: dict = None, method: str = "POST",
            idempotency_key: str = None) -> dict:
    if not config.stripe_ready():
        raise BillingError("Billing is not configured (STRIPE_SECRET_KEY missing).")
    headers = {"Authorization": f"Bearer {config.STRIPE_SECRET_KEY}"}
    url = f"{_API}/{path}"
    data = None
    if method == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(params or {}, doseq=True).encode()
        # Idempotency-Key makes a retried POST (network blip, double-submit) return
        # the SAME object instead of creating a duplicate. Only meaningful on writes.
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        print(f"[caloria] Stripe API error {e.code}: {detail}")   # detail stays server-side
        raise BillingError("Payment processing is temporarily unavailable. Please try again.") from e
    except urllib.error.URLError as e:
        print(f"[caloria] Stripe unreachable: {e.reason}")
        raise BillingError("Payment processing is temporarily unavailable. Please try again.") from e


def _key_mode() -> str:
    """'live' or 'test' for the active secret key. Auto-created product/price ids
    are cached per-mode so switching STRIPE_SECRET_KEY from test to live never
    reuses the test-mode ids (which Stripe rejects under a live key)."""
    return "live" if config.STRIPE_SECRET_KEY.startswith("sk_live") else "test"


def _ensure_price(interval: str, currency: str = None) -> str:
    """Return a Stripe Price id for the interval, denominated in `currency`
    (defaults to the account's BASE_CURRENCY — THB for a Thailand account).

    Resolution:
      1) an explicit Price id configured for this currency (STRIPE_PRICE_*_<CUR>)
      2) for USD only, the legacy single-currency env var (backward compatible)
      3) an auto-created Price IN THIS CURRENCY, cached per currency+mode.

    Crucially the cache key is scoped by currency, so changing BASE_CURRENCY
    (e.g. USD → THB) NEVER returns a stale price of the old currency — a fresh
    price is created in the new currency and the old one is left untouched (so
    existing subscriptions bound to it keep billing normally)."""
    currency = config.normalize_currency(currency or config.BASE_CURRENCY)

    # 1) explicit configured Price id for this exact currency
    pid = config.stripe_price_id(interval, currency)
    if pid:
        return pid
    # 2) legacy single-currency env var — only meaningful for USD (historical)
    if currency == "USD":
        if interval == "monthly" and config.STRIPE_PRICE_MONTHLY:
            return config.STRIPE_PRICE_MONTHLY
        if interval == "yearly" and config.STRIPE_PRICE_YEARLY:
            return config.STRIPE_PRICE_YEARLY

    # 3) auto-create a Price in THIS currency. Cache keys are scoped to
    # currency+mode so switching currency creates a new price instead of reusing
    # an incompatible one.
    # Explicit per-currency prices (derived from config.MONTHLY_PRICE_USD) that
    # override Adaptive Pricing for those markets; Adaptive Pricing converts the
    # THB base for every other currency. Buyers see their local currency either way.
    opts = config.price_currency_options(interval, currency)

    mode = _key_mode()
    # Cache key is scoped by currency AND a signature of the currency_options, so
    # changing the price definition (currency or the per-market amounts) always
    # creates a fresh Price instead of returning an incompatible cached one.
    sig = "-".join(f"{c}{a}" for c, a in sorted(opts.items())) or "single"
    ckey = f"stripe_price_{interval}_{currency}_{sig}_{mode}"
    cached = db.kv_get(ckey)
    if cached:
        return cached

    product_id = db.kv_get(f"stripe_product_{mode}")
    if not product_id:
        # Idempotency-Key is deterministic so two concurrent first-time checkouts
        # can't create two "Caloria Premium" products.
        product = _stripe("products", {"name": "Caloria Premium"},
                          idempotency_key=f"product_caloria_premium_{mode}")
        product_id = product["id"]
        db.kv_set(f"stripe_product_{mode}", product_id)

    amount = config.base_amount(interval, currency)
    recur = "month" if interval == "monthly" else "year"
    params = {
        "product": product_id,
        "unit_amount": amount,
        "currency": currency.lower(),
        "recurring[interval]": recur,
    }
    for cur, amt in opts.items():
        params[f"currency_options[{cur.lower()}][unit_amount]"] = amt
    price = _stripe(
        "prices", params,
        idempotency_key=f"price_{interval}_{currency}_{sig}_{mode}",  # no dup prices under races
    )
    db.kv_set(ckey, price["id"])
    return price["id"]


def _pending_key(user_id) -> str:
    return f"pending_checkout_{user_id}"


def clear_pending_checkout(user_id) -> None:
    """Drop any remembered open Checkout Session for a user (called once they're
    activated, so a later visit never reuses a spent session)."""
    try:
        db.kv_set(_pending_key(user_id), "")
    except Exception as e:  # noqa: BLE001 — cleanup must never break activation
        print(f"[caloria] clear pending checkout failed for {user_id}: {e}")


def create_checkout(user, interval: str, return_base: str = "") -> str:
    if interval not in ("monthly", "yearly", "starter"):
        raise BillingError("Invalid plan interval.")

    # ---- de-duplicate: reuse a recent, still-open session for this user+interval
    # instead of creating a second one. Guards against double-clicks, the browser
    # back button, and request retries all producing separate Checkout Sessions
    # (each of which is a separate PaymentIntent the customer's bank could see as a
    # repeated attempt → card_velocity_exceeded).
    pkey = _pending_key(user["id"])
    cached = db.kv_get(pkey)
    if cached:
        try:
            rec = json.loads(cached)
            if (rec.get("interval") == interval and rec.get("url")
                    and (time.time() - float(rec.get("ts", 0))) < _CHECKOUT_REUSE_SECONDS):
                print(f"[caloria] checkout: reusing session {rec.get('session')} for user {user['id']}")
                return rec["url"]
        except Exception:  # noqa: BLE001 — a bad cache entry just means "create fresh"
            pass

    # Price resolution — ONE source, used by the Checkout Session below:
    #  • If you set an explicit Price id (STRIPE_PRICE_MONTHLY / STRIPE_PRICE_YEARLY)
    #    in the Dashboard, checkout uses EXACTLY that Price — you manage the amount
    #    yourself and this overrides everything below.
    #  • Otherwise we auto-create/reuse ONE base price denominated in the account's
    #    settlement currency (config.BASE_CURRENCY — THB for a Thailand account),
    #    with its amount coming from MONTHLY_PRICE_USD. Because the base currency is
    #    a settlement currency, Stripe Adaptive Pricing presents/charges each buyer
    #    in their own local currency, converting from the base.
    if interval == "starter":
        # Starter uses an EXPLICIT Stripe Price only (never auto-created). It is
        # a monthly recurring price that we schedule to cancel after one cycle.
        price_id = config.STRIPE_PRICE_STARTER
        if not price_id:
            raise BillingError("Starter plan is not configured.")
    else:
        price_id = config.explicit_price_id(interval) or _ensure_price(interval, config.BASE_CURRENCY)
    # Return to the site the request came from (so a private preview deployment
    # lands back on the preview, not the main domain). Only allow-listed origins
    # are honored — anything else falls back to APP_BASE_URL.
    rb = (return_base or "").rstrip("/")
    base = (rb if rb and any(config._origin_matches(rb, p) for p in config.ALLOWED_ORIGINS)
            else config.APP_BASE_URL.rstrip("/"))
    print(f"[caloria] checkout: mode={_key_mode()} interval={interval} price={price_id} return={base}")
    params = {
        "mode": "subscription",
        # Require a card to START the subscription/trial. This is Stripe's default
        # for subscription mode, but we set it EXPLICITLY so the trial can never be
        # created without a payment method (a trial with no PM is what Stripe's
        # account-level "cancel at trial end if no payment method" rule acts on).
        "payment_method_collection": "always",
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": 1,
        "client_reference_id": str(user["id"]),
        "customer_email": user["email"],
        # The {CHECKOUT_SESSION_ID} template is filled in by Stripe on redirect, so
        # the app can confirm the payment server-side immediately — no waiting on
        # the webhook (which stays the source of truth for renewals/cancellations).
        "success_url": f"{base}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/?checkout=cancel",
        "allow_promotion_codes": "true",
        # Stamp the billing interval on both the session and the subscription so
        # the admin dashboard can report monthly vs yearly without extra lookups.
        "metadata[plan_interval]": interval,
        "subscription_data[metadata][plan_interval]": interval,
        # Tier tag read back by the webhook so Starter is never auto-upgraded to
        # Premium on a later subscription.updated event.
        "subscription_data[metadata][tier]": ("starter" if interval == "starter" else "premium"),
    }
    # Reuse this user's existing Stripe customer so a returning subscriber maps to
    # ONE customer (no duplicates) and their history is preserved. A brand-new user
    # has none yet, so Checkout creates one from customer_email. Stripe rejects
    # `customer` and `customer_email` together, so drop the email when reusing.
    existing_customer = _row(user, "stripe_customer")
    if existing_customer:
        params["customer"] = existing_customer
        params.pop("customer_email", None)

    # Free trial — ONE-TIME, new-account-only. trial_eligible() blocks a second
    # trial for anyone who has used one, ever subscribed, or has a Stripe
    # subscription on record (returning customers are charged immediately). The
    # cross-account (same card, new email) case is caught post-checkout by the
    # fingerprint guard in handle_event. trial_period_days is a count of DAYS, so
    # the annual plan gets 3 free days, not 3 free months.
    import auth
    grant_trial = config.TRIAL_DAYS > 0 and auth.trial_eligible(user)
    if grant_trial:
        params["subscription_data[trial_period_days]"] = config.TRIAL_DAYS
        # CRITICAL: pin what happens when the trial ends. Without this, Stripe uses
        # the ACCOUNT's default trial-end behavior — and if that default is "cancel
        # the subscription" (a Dashboard setting), EVERY trial gets scheduled to
        # cancel at trial end with no upcoming invoice instead of converting to a
        # paid month. `create_invoice` forces Stripe to invoice + charge the saved
        # card when the 3 days end, so the trial converts to $19.99/mo and renews.
        params["subscription_data[trial_settings][end_behavior][missing_payment_method]"] = "create_invoice"
        # Stamp both the session and the subscription so the webhook can record
        # trial_used reliably regardless of event ordering.
        params["metadata[trial_granted]"] = "1"
        params["subscription_data[metadata][trial_granted]"] = "1"
    else:
        print(f"[caloria] checkout: no trial for user {user['id']} "
              f"(ineligible/returning) — immediate charge")

    # Deterministic idempotency key bucketed to the reuse window: two requests that
    # slip past the cache check at the same instant still collapse to ONE session
    # at Stripe's side instead of creating duplicates.
    bucket = int(time.time() // _CHECKOUT_REUSE_SECONDS)
    idem = f"checkout_{user['id']}_{interval}_{_key_mode()}_{bucket}"
    session = _stripe("checkout/sessions", params, idempotency_key=idem)

    # Remember it so the very next click reuses this exact session.
    try:
        db.kv_set(pkey, json.dumps({
            "session": session["id"], "url": session["url"],
            "interval": interval, "ts": time.time(),
        }))
    except Exception as e:  # noqa: BLE001 — remembering is best-effort
        print(f"[caloria] remember pending checkout failed for {user['id']}: {e}")
    return session["url"]


def confirm_checkout(user, session_id: str) -> bool:
    """Activate Premium immediately on the post-checkout redirect by verifying the
    Checkout Session directly with Stripe — so the user isn't left waiting on the
    webhook. Idempotent: safe if the webhook already activated. Returns True when
    the account is (now) premium.

    Security: only activates if the session is paid/active AND its
    client_reference_id matches THIS user, so a session id can't be replayed to
    upgrade someone else's account."""
    if not session_id or not config.stripe_ready():
        return False
    try:
        s = _stripe(f"checkout/sessions/{session_id}", method="GET")
    except BillingError as e:
        print(f"[caloria] confirm_checkout lookup failed: {e}")
        return False
    if str(s.get("client_reference_id") or "") != str(user["id"]):
        return False   # session belongs to a different account — ignore
    paid = (s.get("payment_status") == "paid") or (s.get("status") == "complete")
    if not paid:
        return False
    # If a subscription was created, trust its LIVE status over the session's. A
    # trial we canceled for card-reuse (see _enforce_trial_fingerprint) must not be
    # re-activated by this redirect path — only active/trialing subscriptions grant.
    sub_id = s.get("subscription")
    if sub_id and config.stripe_ready():
        try:
            sub = _stripe(f"subscriptions/{sub_id}", method="GET")
            if (sub.get("status") or "").lower() not in _ACTIVE_STATUSES:
                print(f"[caloria] confirm_checkout: subscription {sub_id} is "
                      f"{sub.get('status')} — not activating")
                return False
        except BillingError:
            pass  # transient lookup failure → fall back to session-based activation
    interval = (s.get("metadata") or {}).get("plan_interval")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db.cursor() as c:
        c.execute(
            "UPDATE users SET plan='premium', subscription_status='active', "
            "stripe_customer=COALESCE(?, stripe_customer), "
            "stripe_subscription=COALESCE(?, stripe_subscription), "
            "plan_interval=COALESCE(?, plan_interval), "
            "subscribed_at=COALESCE(subscribed_at, ?) WHERE id=?",
            (s.get("customer"), s.get("subscription"), interval, now, user["id"]),
        )
    clear_pending_checkout(user["id"])
    _log_event("checkout.confirmed", s.get("customer"), "active", user_id=user["id"])
    return True


# ---------- webhooks ----------
def verify_and_parse(payload: bytes, sig_header: str) -> dict:
    """Verify a Stripe webhook signature and return the parsed event."""
    if not config.STRIPE_WEBHOOK_SECRET:
        raise BillingError("STRIPE_WEBHOOK_SECRET not set.")
    parts = dict(
        p.split("=", 1) for p in sig_header.split(",") if "=" in p
    )
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        raise BillingError("Malformed Stripe-Signature header.")
    signed = f"{t}.{payload.decode()}".encode()
    expected = hmac.new(config.STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        raise BillingError("Invalid webhook signature.")
    if abs(time.time() - int(t)) > 60 * 10:
        raise BillingError("Webhook timestamp too old.")
    return json.loads(payload.decode())


# Billing-portal configuration pinned IN CODE so a cancellation always takes
# effect at the END of the current period (the trial end while trialing, or the
# paid-period end once charged) and NEVER immediately — independent of whatever
# the Stripe Dashboard's default portal is set to. `proration_behavior=none`
# means an already-successful payment is never prorated or refunded; the user
# simply keeps access until the period they paid for runs out. Created once, then
# its id is cached in `kv` (and in-process) so we don't recreate it per request.
_PORTAL_CFG_KV = "stripe_portal_cfg_at_period_end_v1"
_portal_cfg_cache = None


def _portal_configuration_id():
    """Return (creating+caching if needed) the id of our at-period-end portal
    configuration. Raises BillingError if Stripe can't create it."""
    global _portal_cfg_cache
    if _portal_cfg_cache:
        return _portal_cfg_cache
    cached = db.kv_get(_PORTAL_CFG_KV)
    if cached:
        _portal_cfg_cache = cached
        return cached
    params = {
        "business_profile[headline]": "Manage your Caloria Club membership",
        # Cancellations are SCHEDULED, not immediate, and never refunded/prorated.
        "features[subscription_cancel][enabled]": "true",
        "features[subscription_cancel][mode]": "at_period_end",
        "features[subscription_cancel][proration_behavior]": "none",
        # Keep the normal self-serve tools the account page already links to.
        "features[invoice_history][enabled]": "true",
        "features[payment_method_update][enabled]": "true",
        "features[customer_update][enabled]": "true",
        "features[customer_update][allowed_updates][]": "email",
        "metadata[managed_by]": "caloria",
        "metadata[cancel_mode]": "at_period_end",
    }
    base = (getattr(config, "APP_BASE_URL", "") or "").rstrip("/")
    if base:
        params["business_profile[privacy_policy_url]"] = f"{base}/privacy.html"
        params["business_profile[terms_of_service_url]"] = f"{base}/terms.html"
    cfg = _stripe("billing_portal/configurations", params, method="POST",
                  idempotency_key="portal_cfg_at_period_end_v1")
    cid = cfg.get("id")
    if not cid:
        # A create that returns no id must NOT be treated as success — otherwise
        # the caller could open an unpinned portal. Fail closed.
        raise BillingError("Could not create the billing portal configuration.")
    db.kv_set(_PORTAL_CFG_KV, cid)
    _portal_cfg_cache = cid
    return cid


def create_portal(user) -> str:
    """Self-serve billing portal: cancel, update card, view invoices.

    FAIL CLOSED: the session is ALWAYS pinned to our at-period-end configuration
    so a cancellation schedules cancel_at_period_end (access continues until the
    period/trial ends) and can never take effect immediately. If that
    configuration cannot be retrieved or created, we raise instead of opening an
    un-pinned portal (which could inherit a Dashboard default that cancels
    immediately). The account page surfaces this as a normal 'temporarily
    unavailable' message via the 503 handler."""
    if not user["stripe_customer"]:
        raise BillingError("No billing account on file.")
    # Propagates BillingError if Stripe can't build/return our configuration —
    # that is the intended fail-closed behavior, do NOT swallow it.
    cfg = _portal_configuration_id()
    if not cfg:
        raise BillingError("Billing portal is temporarily unavailable. Please try again.")
    session = _stripe("billing_portal/sessions", {
        "customer": user["stripe_customer"],
        "return_url": f"{config.APP_BASE_URL}/?billing=done",
        "configuration": cfg,
    })
    return session["url"]


def subscription_info(user) -> dict:
    """Current plan/status/next-billing/renewal for the account-settings UI."""
    info = {
        "plan": user["plan"],
        "status": user["subscription_status"],
        "current_period_end": None,
        "cancel_at_period_end": False,
        "manual": user["subscription_status"] == "manual",
    }
    if config.stripe_ready() and user["stripe_subscription"]:
        try:
            s = _stripe(f"subscriptions/{user['stripe_subscription']}", method="GET")
            info["status"] = s.get("status", info["status"])
            info["current_period_end"] = s.get("current_period_end")
            info["cancel_at_period_end"] = bool(s.get("cancel_at_period_end"))
        except BillingError as e:
            print(f"[caloria] subscription lookup failed: {e}")
    return info



def delete_customer(customer_id: str) -> None:
    """Delete the Stripe customer — cancels subscriptions and avoids orphans."""
    if not (config.stripe_ready() and customer_id):
        return
    _stripe(f"customers/{customer_id}", method="DELETE")


# Subscription statuses that should grant access. 'past_due' keeps access during
# Stripe's automatic retry window (grace period) until it finally cancels.
_ACTIVE_STATUSES = {"active", "trialing", "past_due"}


def _set_by_customer(customer, *, plan, status, subscription=None):
    if not customer:
        return
    with db.cursor() as c:
        if subscription is not None:
            c.execute(
                "UPDATE users SET plan=?, subscription_status=?, stripe_subscription=? "
                "WHERE stripe_customer=?",
                (plan, status, subscription, customer),
            )
        else:
            c.execute(
                "UPDATE users SET plan=?, subscription_status=? WHERE stripe_customer=?",
                (plan, status, customer),
            )


# Terminal states: the subscription is definitively over, so there's no point
# re-asking Stripe (and we must not hammer the API on every request for these).
_TERMINAL_STATUSES = {"canceled", "unpaid", "incomplete_expired"}


def _row(user, key, default=None):
    try:
        v = user[key]
        return default if v is None else v
    except (KeyError, IndexError):
        return default


def live_subscription_active(user):
    """Ask Stripe (the source of truth) whether this user's subscription is
    currently active/trialing/past_due. Returns True/False, or None when it
    can't be determined (Stripe not configured, no subscription id, or API
    error) so callers fall back to the cached state."""
    sub_id = _row(user, "stripe_subscription")
    if not (config.stripe_ready() and sub_id):
        return None
    try:
        s = _stripe(f"subscriptions/{sub_id}", method="GET")
    except BillingError as e:
        print(f"[caloria] live subscription check failed: {e}")
        return None
    return (s.get("status") or "").lower() in _ACTIVE_STATUSES


def reconcile_entitlement(user) -> bool:
    """Self-heal a stale 'free'/non-entitled row for a customer whose Stripe
    subscription is in fact still active (e.g. a missed or out-of-order webhook).
    Confirms with Stripe and, only if Stripe says active, repairs the cached
    plan/status and returns True.

    This can ONLY grant access that Stripe confirms — it never downgrades anyone
    and never masks a real cancellation, so it is safe for all other customers.
    Definitively-terminal statuses are trusted as-is (no Stripe call)."""
    status = str(_row(user, "subscription_status", "") or "").lower()
    if status in _TERMINAL_STATUSES:
        return False
    if live_subscription_active(user) is not True:
        return False
    customer = _row(user, "stripe_customer")
    if customer:
        _set_by_customer(customer, plan="premium", status="active",
                         subscription=_row(user, "stripe_subscription"))
    else:
        with db.cursor() as c:
            c.execute("UPDATE users SET plan='premium', subscription_status='active' WHERE id=?",
                      (user["id"],))
    print(f"[caloria] reconciled entitlement from Stripe for user={user['id']}")
    return True


def _log_event(etype, customer, status, user_id=None):
    """Append to the billing event log (subscription history & support)."""
    try:
        with db.cursor() as c:
            if user_id is None and customer:
                row = c.execute(
                    "SELECT id FROM users WHERE stripe_customer = ?", (customer,)
                ).fetchone()
                user_id = row["id"] if row else None
            c.execute(
                "INSERT INTO billing_events (user_id, customer, type, status) VALUES (?,?,?,?)",
                (user_id, customer, etype, status),
            )
    except Exception as e:  # noqa: BLE001 — logging must never break webhook handling
        print(f"[caloria] billing event log failed: {e}")


def _is_starter_customer(customer) -> bool:
    if not customer:
        return False
    with db.cursor() as c:
        r = c.execute("SELECT plan FROM users WHERE stripe_customer=?", (customer,)).fetchone()
    return bool(r and (r["plan"] or "") == "starter")


def _send_starter_expiry_by_customer(customer) -> None:
    if not customer:
        return
    with db.cursor() as c:
        r = c.execute("SELECT id FROM users WHERE stripe_customer=?", (customer,)).fetchone()
    if r:
        _send_starter_expiry(r["id"])


def _send_starter_expiry(user_id: int) -> None:
    """Send the 'your first month has ended' email, with a Premium upgrade link.
    Claims the dedupe row FIRST so a duplicate subscription.deleted webhook can
    never send twice; email_send retries transient failures internally."""
    with db.cursor() as c:
        claimed = c.execute(
            "INSERT OR IGNORE INTO starter_expiry_sends (user_id) VALUES (?)", (user_id,)
        ).rowcount > 0
    if not claimed:
        return                                   # already sent — dedupe
    with db.cursor() as c:
        r = c.execute("SELECT email, name FROM users WHERE id=?", (user_id,)).fetchone()
    if not r or not r["email"]:
        return
    try:
        email_send.send_starter_expired(r["email"], r["name"])
        print(f"[caloria] starter expiry email sent user={user_id}")
    except Exception as e:  # noqa: BLE001 — logged; dedupe row stays claimed
        print(f"[caloria] starter expiry email failed user={user_id}: {e}")


def _pm_fingerprint(pm):
    """Card fingerprint from a payment-method object OR its id (fetches when given an
    id). Returns None for non-card methods, wallets with no card fingerprint, or any
    error (never raises)."""
    try:
        if isinstance(pm, str) and pm:
            pm = _stripe(f"payment_methods/{pm}", method="GET")
        if isinstance(pm, dict):
            return (pm.get("card") or {}).get("fingerprint")
    except BillingError:
        return None
    return None


def _customer_default_card_fingerprint(customer: str):
    """Fingerprint of the customer's CHOSEN DEFAULT card (invoice_settings
    .default_payment_method) — NOT merely 'the first card on the account'. Used only
    as a last-resort fallback; the subscription's own payment method is preferred.
    None on failure (never raises)."""
    if not (customer and config.stripe_ready()):
        return None
    try:
        cust = _stripe(f"customers/{customer}",
                       {"expand[]": "invoice_settings.default_payment_method"},
                       method="GET")
        return _pm_fingerprint((cust.get("invoice_settings") or {}).get("default_payment_method"))
    except BillingError:
        return None


def _subscription_card_fingerprint(subscription_id: str, customer: str = None):
    """Fingerprint of the card ACTUALLY attached to THIS subscription, so the guard
    keys on the real payment method for the specific trial — never an unrelated card
    that merely happens to be the first one on the Stripe customer. Resolution order:
      1) the subscription's own default_payment_method,
      2) the card that paid its latest invoice (payment_intent.payment_method),
      3) last resort: the customer's chosen default card.
    Returns None when nothing resolves — e.g. Apple Pay / Google Pay tokenized
    wallets expose no stable card fingerprint (never raises)."""
    if not (subscription_id and config.stripe_ready()):
        return None
    sub = {}
    try:
        sub = _stripe(f"subscriptions/{subscription_id}",
                      {"expand[]": "default_payment_method"}, method="GET")
        fp = _pm_fingerprint(sub.get("default_payment_method"))
        if fp:
            return fp
    except BillingError:
        pass
    try:
        sub2 = _stripe(f"subscriptions/{subscription_id}",
                       {"expand[]": "latest_invoice.payment_intent.payment_method"},
                       method="GET")
        pi = ((sub2.get("latest_invoice") or {}).get("payment_intent") or {})
        fp = _pm_fingerprint(pi.get("payment_method"))
        if fp:
            return fp
    except BillingError:
        pass
    return _customer_default_card_fingerprint(customer or sub.get("customer"))


def _user_id_for_customer(customer: str):
    """Owning user id for a Stripe customer (for the fingerprint ledger audit column);
    None if unknown. Never raises."""
    if not customer:
        return None
    try:
        with db.cursor() as c:
            r = c.execute("SELECT id FROM users WHERE stripe_customer=?", (customer,)).fetchone()
        return r["id"] if r else None
    except Exception:  # noqa: BLE001
        return None


def _enforce_trial_fingerprint(customer: str, subscription_id: str) -> None:
    """One 3-day trial per PHYSICAL CARD, across accounts/emails, race-safe.

    The fingerprint comes from the ACTUAL payment method on THIS trialing
    subscription (see _subscription_card_fingerprint), not merely the first card on
    the customer. Claiming the card is a single ATOMIC `INSERT OR IGNORE` on the
    fingerprint PRIMARY KEY: under two simultaneous trial attempts with the same
    card, exactly one INSERT wins (keeps its trial) and every other loses and is
    treated as reuse — they can never both receive a free trial.

    FAIL-OPEN everywhere: if no card fingerprint can be read (incl. Apple Pay /
    Google Pay tokenized cards) or any lookup fails, take NO action — never cancel a
    subscription, never block a paid checkout.

    On confirmed reuse (card already owned by a DIFFERENT customer): LOG-ONLY mode
    (default) only logs; ENFORCE mode re-verifies the NEW subscription is still
    `trialing`, then DELETEs ONLY that subscription (no invoice, no charge) and marks
    ONLY the new account free. The original owner's subscription is never read or
    modified, so existing subscriptions and their billing dates are untouched."""
    if not subscription_id:
        return
    fp = _subscription_card_fingerprint(subscription_id, customer)
    if not fp:
        # No stable card fingerprint (e.g. Apple Pay / Google Pay wallet, or lookup
        # failure) → this cannot be a per-card decision. Do nothing: never cancel,
        # never block, never charge.
        print(f"[caloria] trial: no card fingerprint for sub {subscription_id} "
              f"(customer {customer}) — fingerprint guard skipped (fail-open)")
        return

    # ---- Atomic claim. The fingerprint PRIMARY KEY makes this race-safe across
    # concurrent webhooks/workers: the first INSERT wins, all others are IGNOREd. ----
    owner_user_id = _user_id_for_customer(customer)   # resolved BEFORE the cursor (lock) block
    try:
        with db.cursor() as c:
            c.execute(
                "INSERT OR IGNORE INTO trial_fingerprints (fingerprint, customer, subscription, user_id) "
                "VALUES (?, ?, ?, ?)",
                (fp, customer, subscription_id, owner_user_id),
            )
            claimed = (c.rowcount == 1)
            owner = None if claimed else c.execute(
                "SELECT customer FROM trial_fingerprints WHERE fingerprint=?", (fp,)
            ).fetchone()
    except Exception as e:  # noqa: BLE001 — never break the webhook
        print(f"[caloria] trial: fingerprint claim failed (fail-open): {e}")
        return

    if claimed:
        # First trial ever seen for this physical card → legitimate; keep it.
        print(f"[caloria] trial: card {fp[:8]}… first trial recorded for customer {customer}")
        return

    owner_customer = owner["customer"] if owner else None
    if not owner_customer or owner_customer == customer:
        # Same customer (duplicate webhook / same account) → no action.
        return

    # ---- Confirmed cross-account reuse of the same physical card ----
    if not config.TRIAL_FINGERPRINT_ENFORCE:
        # LOG-ONLY (default): detect + log, take NO action.
        print(f"[caloria] trial: LOG-ONLY fingerprint reuse ({fp[:8]}…) — WOULD cancel "
              f"trial for sub {subscription_id} (customer {customer}); no action taken "
              f"(set TRIAL_FINGERPRINT_ENFORCE=true to enforce)")
        _log_event("trial.fingerprint_reuse_logonly", customer, "detected_no_action")
        return

    # ENFORCE: re-verify THIS subscription is STILL trialing before cancelling, so a
    # converted/active subscription — and its billing date — can never be touched.
    try:
        live = _stripe(f"subscriptions/{subscription_id}", method="GET")
    except BillingError as e:
        print(f"[caloria] trial: could not re-verify sub {subscription_id} — not cancelling ({e})")
        return
    if (live.get("status") or "").lower() != "trialing":
        print(f"[caloria] trial: sub {subscription_id} is {live.get('status')} (not trialing) "
              f"— not cancelling (billing untouched)")
        return

    # Cancel ONLY this duplicate trial, with NO invoice/charge.
    try:
        _stripe(f"subscriptions/{subscription_id}", method="DELETE",
                idempotency_key=f"trialblock_{subscription_id}")
        print(f"[caloria] trial: fingerprint reuse ({fp[:8]}…) — canceled duplicate trial "
              f"(no charge) for sub {subscription_id}")
        _log_event("trial.fingerprint_reuse", customer, "trial_blocked_no_charge")
    except BillingError as e:
        print(f"[caloria] trial: could not cancel reused trial {subscription_id}: {e}")
        return
    # Mark ONLY the new (duplicate) account free. The subscription.deleted webhook
    # this DELETE emits converges the cached plan regardless of event ordering.
    try:
        with db.cursor() as c:
            c.execute("UPDATE users SET trial_used=1, plan='free', "
                      "subscription_status='canceled' WHERE stripe_customer=?",
                      (customer,))
    except Exception as e:  # noqa: BLE001
        print(f"[caloria] trial: could not mark trial_used after block: {e}")


def handle_event(event: dict) -> None:
    etype = event.get("type")
    obj = event.get("data", {}).get("object", {})
    _log_event(etype, obj.get("customer"), obj.get("status"),
               user_id=int(obj["client_reference_id"]) if obj.get("client_reference_id") else None)

    if etype == "checkout.session.completed":
        user_id = obj.get("client_reference_id")
        if user_id:
            interval = (obj.get("metadata") or {}).get("plan_interval")
            is_starter = interval == "starter"
            new_plan = "starter" if is_starter else "premium"
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with db.cursor() as c:
                c.execute(
                    "UPDATE users SET plan=?, subscription_status='active', "
                    "stripe_customer=?, stripe_subscription=?, "
                    "plan_interval=COALESCE(?, plan_interval), "
                    "subscribed_at=COALESCE(subscribed_at, ?)"
                    + (", starter_used=1" if is_starter else "")
                    + " WHERE id=?",
                    (new_plan, obj.get("customer"), obj.get("subscription"), interval, now, int(user_id)),
                )
            # One-time trial guard: if THIS checkout granted a trial (stamped at
            # create_checkout), mark the account so a later checkout can't get a
            # second one. Keyed by user_id, so it is reliable regardless of order.
            if (obj.get("metadata") or {}).get("trial_granted") == "1":
                with db.cursor() as c:
                    c.execute("UPDATE users SET trial_used=1 WHERE id=?", (int(user_id),))
            clear_pending_checkout(int(user_id))
            if is_starter and obj.get("subscription") and config.TRIAL_DAYS <= 0:
                # No trial: make it NON-RENEWING now — bill this one month, then
                # cancel. (WITH a trial the subscription is still `trialing` here,
                # and cancel_at_period_end would cancel at the TRIAL end and never
                # charge — so for the trial case we defer this to the moment the
                # trial converts to a paid month: see customer.subscription.updated.)
                try:
                    _stripe(f"subscriptions/{obj['subscription']}",
                            {"cancel_at_period_end": "true"}, method="POST")
                    print(f"[caloria] starter: scheduled non-renewal for user {user_id}")
                except BillingError as e:
                    print(f"[caloria] starter: could not schedule non-renewal: {e}")

    elif etype == "customer.subscription.created":
        # A new subscription. If it started in a trial, run the cross-account guard:
        # a card that already consumed a trial (on ANY customer) has its trial ended
        # now so Stripe charges immediately. Also stamp trial_used on the owning
        # account as a backup to checkout.session.completed (webhook order varies).
        customer = obj.get("customer")
        if obj.get("status") == "trialing" and customer:
            _enforce_trial_fingerprint(customer, obj.get("id"))
        if (obj.get("metadata") or {}).get("trial_granted") == "1" and customer:
            with db.cursor() as c:
                c.execute("UPDATE users SET trial_used=1 WHERE stripe_customer=?", (customer,))

    elif etype == "customer.subscription.trial_will_end":
        # Informational reminder (~3 days before trial end; for a 3-day trial it is
        # effectively immediate). The actual trial→paid conversion arrives as
        # customer.subscription.updated, so we only log here — no plan change, no
        # email (wire a reminder later if desired).
        print(f"[caloria] trial_will_end for customer {obj.get('customer')}")

    elif etype == "customer.subscription.updated":
        # Tier-aware: a Starter subscription stays Starter (never auto-upgraded).
        status = obj.get("status", "")
        starter = (obj.get("metadata") or {}).get("tier") == "starter" or _is_starter_customer(obj.get("customer"))
        if status in _ACTIVE_STATUSES:
            plan = "starter" if starter else "premium"
        else:
            plan = "free"
        _set_by_customer(obj.get("customer"), plan=plan, status=status, subscription=obj.get("id"))
        # Starter = ONE paid month, non-renewing. When its trial converts to a
        # paying month (status leaves `trialing` for `active`), schedule the
        # end-of-period cancellation exactly once — current_period_end is now the
        # end of the FIRST PAID month, so the customer is billed once then stops.
        # Idempotent: skipped if already set. Premium/Annual are never scheduled.
        if (starter and status == "active" and obj.get("id")
                and not obj.get("cancel_at_period_end") and config.stripe_ready()):
            try:
                _stripe(f"subscriptions/{obj['id']}",
                        {"cancel_at_period_end": "true"}, method="POST")
                print(f"[caloria] starter: scheduled non-renewal (post-trial) for sub {obj['id']}")
            except BillingError as e:
                print(f"[caloria] starter: could not schedule post-trial non-renewal: {e}")

    elif etype in ("customer.subscription.deleted", "customer.subscription.canceled"):
        starter = (obj.get("metadata") or {}).get("tier") == "starter" or _is_starter_customer(obj.get("customer"))
        _set_by_customer(obj.get("customer"), plan="free", status="canceled")
        if starter:
            # Starter's one month has ended — send the expiration email (deduped).
            _send_starter_expiry_by_customer(obj.get("customer"))

    elif etype == "invoice.payment_failed":
        # Enter grace period; keep access while Stripe retries the card.
        _set_by_customer(obj.get("customer"), plan="premium", status="past_due")

    elif etype in ("invoice.paid", "invoice.payment_succeeded"):
        # Preserve a Starter tier on its (single) paid invoice; else Premium.
        plan = "starter" if _is_starter_customer(obj.get("customer")) else "premium"
        _set_by_customer(obj.get("customer"), plan=plan, status="active")
