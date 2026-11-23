#!/usr/bin/env python3
"""Stripe TEST-MODE verification for the Caloria trial/subscription flow.

Run this with your Stripe **TEST** secret key to get real, Stripe-side evidence
that the billing code behaves correctly. It will REFUSE to run against a live key.
Pure stdlib; no dependencies. Nothing is printed except the key's test objects.

WHAT IT PROVES
  PART A — Parameter evidence (deterministic, offline-to-Stripe):
    Drives the REAL `billing.create_checkout()` with a fake eligible user and a
    fake ineligible user (Stripe calls are intercepted), and asserts the exact
    Checkout params we would send Stripe:
      • eligible monthly  → subscription_data[trial_period_days]=3
                          + trial_settings[end_behavior][missing_payment_method]=create_invoice
      • ineligible/return → NO trial_period_days (immediate charge)
      • both              → payment_method_collection=always, correct price id, tier metadata

  PART B — Live Test-Clock lifecycle (requires network + TEST key):
    Creates a throwaway $19.99/mo TEST price, a test customer on a Stripe Test
    Clock with a test card, and a subscription mirroring our ELIGIBLE params
    (trial_period_days=3, end_behavior=create_invoice). Then:
      • asserts status == trialing and NO charge during the trial,
      • advances the clock 3 days + 1h,
      • asserts status == active and the FIRST $19.99 invoice is paid,
      • creates a NO-TRIAL subscription and asserts an IMMEDIATE paid invoice.
    Finally DELETES the test clock (removing all objects it created).

USAGE
    cd backend-deploy
    STRIPE_SECRET_KEY=sk_test_xxx python3 stripe_testmode_check.py
    STRIPE_SECRET_KEY=sk_test_xxx python3 stripe_testmode_check.py --params-only   # Part A only
"""
import os
import sys
import json
import time
import urllib.parse
import urllib.request
import urllib.error

API = "https://api.stripe.com/v1"
KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()


def die(msg):
    print(f"ABORT: {msg}")
    sys.exit(2)


if not KEY:
    die("STRIPE_SECRET_KEY not set. Use your TEST key (sk_test_...).")
if KEY.startswith("sk_live"):
    die("Refusing to run against a LIVE key. Use your TEST key (sk_test_...).")


def api(path, params=None, method="POST"):
    url = f"{API}/{path}"
    headers = {"Authorization": f"Bearer {KEY}"}
    data = None
    if method == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(params or {}, doseq=True).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode('utf-8','ignore')[:300]}")


PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


# ---------------------------------------------------------------------------
# PART A — what our code actually sends Stripe (intercepted; deterministic)
# ---------------------------------------------------------------------------
def part_a():
    print("\n=== PART A — Checkout parameter evidence (our real create_checkout) ===")
    import tempfile
    os.environ.setdefault("CALORIA_DB",
                          tempfile.NamedTemporaryFile(prefix="tmchk_", suffix=".db", delete=False).name)
    os.environ.setdefault("STRIPE_PRICE_MONTHLY", "price_monthly_testcfg")
    os.environ.setdefault("STRIPE_PRICE_YEARLY", "price_yearly_testcfg")
    os.environ.setdefault("TRIAL_DAYS", "3")
    os.environ.setdefault("TRIAL_LAUNCH_AT", "2026-11-03")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, db, auth, billing  # noqa: E402
    db.init_db()

    captured = {}

    def fake_stripe(path, params=None, method="POST", idempotency_key=None):
        if path == "checkout/sessions":
            captured["params"] = dict(params or {})
            return {"id": "cs_test_capture", "url": "https://checkout.stripe.test/capture"}
        return {"id": "obj_test", "url": "https://t/x"}

    billing._stripe = fake_stripe

    import datetime
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    def mkuser(**cols):
        base = {"email": cols.pop("email", f"a{time.time_ns()}@test.dev"), "name": "T",
                "pw_salt": "s", "pw_hash": "h", "plan": "free", "created_at": now}
        base.update(cols)
        keys = ", ".join(base); qs = ", ".join("?" for _ in base)
        with db.cursor() as c:
            return c.execute(f"INSERT INTO users ({keys}) VALUES ({qs})", tuple(base.values())).lastrowid

    # Eligible
    uid = mkuser()
    u = auth._get_user(uid)
    billing.create_checkout(u, "monthly", trial_eligible=auth.trial_eligible(u))
    p = captured["params"]
    check(p.get("subscription_data[trial_period_days]") == 3,
          "eligible monthly → trial_period_days=3")
    check(p.get("subscription_data[trial_settings][end_behavior][missing_payment_method]") == "create_invoice",
          "eligible → end_behavior=create_invoice (converts to paid, never auto-cancels)")
    check(p.get("payment_method_collection") == "always",
          "eligible → payment_method_collection=always (card captured, not charged)")
    check(p.get("subscription_data[metadata][tier]") == "premium", "eligible → tier=premium")

    # Ineligible / returning
    uid2 = mkuser(trial_used=1)
    u2 = auth._get_user(uid2)
    billing.create_checkout(u2, "monthly", trial_eligible=auth.trial_eligible(u2))
    p2 = captured["params"]
    check("subscription_data[trial_period_days]" not in p2,
          "returning → NO trial_period_days (immediate charge)")
    check(p2.get("payment_method_collection") == "always",
          "returning → payment_method_collection=always")


# ---------------------------------------------------------------------------
# PART B — live Stripe Test Clock lifecycle
# ---------------------------------------------------------------------------
def _wait_clock_ready(clock_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        c = api(f"test_helpers/test_clocks/{clock_id}", method="GET")
        if c.get("status") == "ready":
            return c
        if c.get("status") == "internal_failure":
            raise RuntimeError("test clock internal_failure")
        time.sleep(3)
    raise RuntimeError("test clock did not become ready in time")


def _paid_invoices(customer_id):
    """All PAID invoices for a customer (test mode)."""
    out, after = [], None
    while True:
        p = {"customer": customer_id, "status": "paid", "limit": 100}
        if after:
            p["starting_after"] = after
        page = api("invoices", p, method="GET")
        data = page.get("data", [])
        out.extend(data)
        if not page.get("has_more") or not data:
            return out
        after = data[-1]["id"]


def _charged(customer_id):
    """PAID invoices that actually MOVED MONEY (amount_paid > 0). Excludes the $0
    subscription-create invoice Stripe auto-issues (and marks paid) for a trial."""
    return [i for i in _paid_invoices(customer_id) if (i.get("amount_paid") or 0) > 0]


def _subscription_count(customer_id):
    """How many subscriptions (any status) the customer has — a duplicate-sub guard."""
    n, after = 0, None
    while True:
        p = {"customer": customer_id, "status": "all", "limit": 100}
        if after:
            p["starting_after"] = after
        page = api(f"subscriptions", p, method="GET")
        data = page.get("data", [])
        n += len(data)
        if not page.get("has_more") or not data:
            return n
        after = data[-1]["id"]


def _print_ledger(stage, customer_id):
    """Trace EVERY invoice (id, time, billing_reason, status, due, paid, sub) so the
    $0 trial-create invoice vs the real $19.99 charges are visible and auditable."""
    print(f"    invoice ledger [{stage}] customer={customer_id}:")
    all_inv, after = [], None
    while True:
        p = {"customer": customer_id, "limit": 100}
        if after:
            p["starting_after"] = after
        page = api("invoices", p, method="GET")
        data = page.get("data", [])
        all_inv.extend(data)
        if not page.get("has_more") or not data:
            break
        after = data[-1]["id"]
    if not all_inv:
        print("      (no invoices)")
    for i in sorted(all_inv, key=lambda x: x.get("created") or 0):
        print(f"      {i.get('id')}  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(i.get('created') or 0))}"
              f"  reason={i.get('billing_reason')}  status={i.get('status')}"
              f"  due=${(i.get('amount_due') or 0)/100:.2f}  paid=${(i.get('amount_paid') or 0)/100:.2f}"
              f"  sub={i.get('subscription')}")


def part_b():
    print("\n=== PART B — live Test-Clock lifecycle (TEST mode) ===")
    now = int(time.time())
    clock = api("test_helpers/test_clocks", {"frozen_time": now, "name": "caloria-trial-check"})
    clock_id = clock["id"]
    print(f"  test clock: {clock_id}")
    try:
        prod = api("products", {"name": "Caloria TEST Premium (throwaway)"})
        price = api("prices", {"product": prod["id"], "unit_amount": 1999,
                               "currency": "usd", "recurring[interval]": "month"})
        print(f"  throwaway price: {price['id']} ($19.99/mo)")

        cust = api("customers", {"test_clock": clock_id, "email": "clocktest@test.dev"})
        pm = api("payment_methods", {"type": "card", "card[token]": "tok_visa"})
        api(f"payment_methods/{pm['id']}/attach", {"customer": cust["id"]})
        api(f"customers/{cust['id']}", {"invoice_settings[default_payment_method]": pm["id"]})

        # ELIGIBLE: 3-day trial, our end_behavior
        sub = api("subscriptions", {
            "customer": cust["id"], "items[0][price]": price["id"],
            "trial_period_days": 3,
            "trial_settings[end_behavior][missing_payment_method]": "create_invoice",
            "expand[]": "latest_invoice",
        })
        check(sub.get("status") == "trialing", f"eligible sub starts trialing (got {sub.get('status')})")
        trial_end = sub.get("trial_end") or 0
        check(2 * 86400 <= (trial_end - now) <= 4 * 86400,
              f"trial_end ≈ now+3d (got +{(trial_end - now)/86400:.1f}d)")
        inv = sub.get("latest_invoice") or {}
        check((inv.get("amount_paid") or 0) == 0, "NO charge on the trial-start invoice")
        # Stronger, measured across ALL invoices: no REAL money moved during the
        # trial. Stripe DOES create a $0 invoice at subscription-create
        # (billing_reason=subscription_create) and marks it 'paid' — that is
        # expected and is NOT a charge, so we only ever count invoices that actually
        # took money (amount_paid > 0). This is what the earlier version miscounted.
        real0 = _charged(cust["id"])
        check(len(real0) == 0, f"NO real (>$0) charge during the trial (got {len(real0)})")
        _print_ledger("at trial start", cust["id"])

        # advance the clock exactly past the 3-day trial
        api(f"test_helpers/test_clocks/{clock_id}/advance", {"frozen_time": now + 3 * 86400 + 3600})
        _wait_clock_ready(clock_id)
        sub2 = api(f"subscriptions/{sub['id']}", {"expand[]": "latest_invoice"}, method="GET")
        check(sub2.get("status") == "active", f"after 3 days → active (got {sub2.get('status')})")
        real1 = _charged(cust["id"])
        check(len(real1) == 1, f"exactly ONE real charge after the trial — no double-charge (got {len(real1)})")
        check(all(i.get("amount_paid") == 1999 for i in real1), "the post-trial charge is exactly $19.99")
        check(_subscription_count(cust["id"]) == 1,
              "exactly ONE subscription on the customer — no duplicate subscription")
        _print_ledger("after trial -> active", cust["id"])

        # RENEWAL: advance one full billing period and verify a SECOND $19.99 charge.
        period_end = sub2.get("current_period_end") or (now + 33 * 86400)
        api(f"test_helpers/test_clocks/{clock_id}/advance", {"frozen_time": period_end + 3600})
        _wait_clock_ready(clock_id)
        sub2b = api(f"subscriptions/{sub['id']}", {"expand[]": "latest_invoice"}, method="GET")
        check(sub2b.get("status") == "active", f"after renewal → still active (got {sub2b.get('status')})")
        real2 = _charged(cust["id"])
        check(len(real2) == 2, f"exactly TWO real charges after one renewal — no double-charge (got {len(real2)})")
        check(all(i.get("amount_paid") == 1999 for i in real2), "each charge is exactly $19.99")
        check(_subscription_count(cust["id"]) == 1, "still exactly ONE subscription after renewal")
        _print_ledger("after renewal", cust["id"])

        # INELIGIBLE: no trial → immediate charge
        cust2 = api("customers", {"test_clock": clock_id, "email": "clocktest2@test.dev"})
        pm2 = api("payment_methods", {"type": "card", "card[token]": "tok_visa"})
        api(f"payment_methods/{pm2['id']}/attach", {"customer": cust2["id"]})
        api(f"customers/{cust2['id']}", {"invoice_settings[default_payment_method]": pm2["id"]})
        sub3 = api("subscriptions", {
            "customer": cust2["id"], "items[0][price]": price["id"],
            "expand[]": "latest_invoice",
        })
        check(sub3.get("status") == "active", f"no-trial sub is active immediately (got {sub3.get('status')})")
        real3 = _charged(cust2["id"])
        check(len(real3) == 1 and real3[0].get("amount_paid") == 1999,
              f"no-trial sub → exactly ONE immediate $19.99 charge (got {len(real3)})")
        check(_subscription_count(cust2["id"]) == 1, "no-trial customer has exactly ONE subscription")
        _print_ledger("no-trial customer", cust2["id"])
    finally:
        try:
            api(f"test_helpers/test_clocks/{clock_id}", method="DELETE")
            print(f"  cleaned up test clock {clock_id} (and its test customers/subs)")
        except Exception as e:  # noqa: BLE001
            print(f"  NOTE: could not auto-delete test clock {clock_id}: {e}")


def main():
    print(f"=== Caloria Stripe TEST-MODE check (key mode: "
          f"{'TEST' if KEY.startswith('sk_test') else 'UNKNOWN'}) ===")
    part_a()
    if "--params-only" not in sys.argv:
        try:
            part_b()
        except Exception as e:  # noqa: BLE001
            FAIL.append(f"Part B error: {e}")
            print(f"  FAIL Part B raised: {e}")
    print("\n=== SUMMARY ===")
    print(f"passed: {len(PASS)}   failed: {len(FAIL)}")
    for f in FAIL:
        print(f"  - FAIL: {f}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
