"""Stripe subscriptions (stdlib only).

Creates Checkout Sessions for the Monthly ($19.99) and Yearly ($99) plans, and
verifies webhooks to upgrade/downgrade accounts. Prices can be supplied via env
(STRIPE_PRICE_MONTHLY / STRIPE_PRICE_YEARLY) or auto-created on first use and
cached in the kv table.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import db

_API = "https://api.stripe.com/v1"


class BillingError(RuntimeError):
    pass


def _stripe(path: str, params: dict = None, method: str = "POST") -> dict:
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


def _ensure_price(interval: str, currency: str = "USD") -> str:
    """Return a Stripe price id for the interval in the requested currency.

    Resolution order: localized price id for the currency -> USD price id ->
    legacy single-currency env var -> auto-created USD price. This means a
    currency without a configured price simply falls back to USD.
    """
    currency = config.normalize_currency(currency)

    # 1) localized price id for the requested currency (e.g. THB/EUR/GBP)
    pid = config.stripe_price_id(interval, currency)
    if pid:
        return pid
    # 2) fall back to the USD localized price id
    pid = config.stripe_price_id(interval, "USD")
    if pid:
        return pid
    # 3) legacy single-currency env var (backward compatible)
    if interval == "monthly" and config.STRIPE_PRICE_MONTHLY:
        return config.STRIPE_PRICE_MONTHLY
    if interval == "yearly" and config.STRIPE_PRICE_YEARLY:
        return config.STRIPE_PRICE_YEARLY

    # 4) auto-create a USD price. Cache keys are scoped to the key mode (live/test)
    # so a test->live switch creates fresh live objects instead of reusing
    # incompatible test-mode ids.
    mode = _key_mode()
    cached = db.kv_get(f"stripe_price_{interval}_{mode}")
    if cached:
        return cached

    product_id = db.kv_get(f"stripe_product_{mode}")
    if not product_id:
        product = _stripe("products", {"name": "Caloria Premium"})
        product_id = product["id"]
        db.kv_set(f"stripe_product_{mode}", product_id)

    amount = 1999 if interval == "monthly" else 9900
    recur = "month" if interval == "monthly" else "year"
    price = _stripe(
        "prices",
        {
            "product": product_id,
            "unit_amount": amount,
            "currency": "usd",
            "recurring[interval]": recur,
        },
    )
    db.kv_set(f"stripe_price_{interval}_{mode}", price["id"])
    return price["id"]


def create_checkout(user, interval: str, return_base: str = "") -> str:
    if interval not in ("monthly", "yearly"):
        raise BillingError("Invalid plan interval.")
    # ONE base price (USD). We never pin a currency here — Stripe Adaptive Pricing
    # (enabled on the account) automatically presents and charges each customer in
    # their local currency at checkout. Passing a currency-specific price would
    # DISABLE Adaptive Pricing, so we deliberately always use the base price.
    price_id = _ensure_price(interval)
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
    session = _stripe("checkout/sessions", params)
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
