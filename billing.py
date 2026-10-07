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
    if interval not in ("monthly", "yearly"):
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
    }
    # Free trial — no charge until the trial ends; cancel anytime before then.
    if config.TRIAL_DAYS > 0:
        params["subscription_data[trial_period_days]"] = config.TRIAL_DAYS

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


def create_portal(user) -> str:
    """Self-serve billing portal: cancel, update card, view invoices."""
    if not user["stripe_customer"]:
        raise BillingError("No billing account on file.")
    session = _stripe("billing_portal/sessions", {
        "customer": user["stripe_customer"],
        "return_url": f"{config.APP_BASE_URL}/?billing=done",
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


def handle_event(event: dict) -> None:
    etype = event.get("type")
    obj = event.get("data", {}).get("object", {})
    _log_event(etype, obj.get("customer"), obj.get("status"),
               user_id=int(obj["client_reference_id"]) if obj.get("client_reference_id") else None)

    if etype == "checkout.session.completed":
        user_id = obj.get("client_reference_id")
        if user_id:
            interval = (obj.get("metadata") or {}).get("plan_interval")
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            with db.cursor() as c:
                c.execute(
                    "UPDATE users SET plan='premium', subscription_status='active', "
                    "stripe_customer=?, stripe_subscription=?, "
                    "plan_interval=COALESCE(?, plan_interval), "
                    "subscribed_at=COALESCE(subscribed_at, ?) WHERE id=?",
                    (obj.get("customer"), obj.get("subscription"), interval, now, int(user_id)),
                )
            clear_pending_checkout(int(user_id))

    elif etype == "customer.subscription.updated":
        # Source of truth for status changes (trial→active, past_due, paused, etc.).
        status = obj.get("status", "")
        plan = "premium" if status in _ACTIVE_STATUSES else "free"
        _set_by_customer(obj.get("customer"), plan=plan, status=status, subscription=obj.get("id"))

    elif etype in ("customer.subscription.deleted", "customer.subscription.canceled"):
        _set_by_customer(obj.get("customer"), plan="free", status="canceled")

    elif etype == "invoice.payment_failed":
        # Enter grace period; keep access while Stripe retries the card.
        _set_by_customer(obj.get("customer"), plan="premium", status="past_due")

    elif etype in ("invoice.paid", "invoice.payment_succeeded"):
        _set_by_customer(obj.get("customer"), plan="premium", status="active")
