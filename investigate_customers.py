#!/usr/bin/env python3
"""READ-ONLY per-customer investigation of the billing-bug-affected subscriptions.

STRICTLY READ-ONLY. It makes ONLY Stripe GET requests and opens the local SQLite
DB in read-only mode (`mode=ro`). It never creates, updates, cancels, refunds,
charges, or deletes anything in Stripe or the database. Pure stdlib.

    python3 investigate_customers.py

For every affected subscription (same three groups as audit_customers.py),
deduplicated per customer, it prints:
  • Stripe customer: id, email, name, created
  • Subscription: id, status, tier(metadata), price, created, trial_start/end,
    current_period_start/end, cancel_at_period_end, cancel_at, canceled_at,
    cancellation_reason
  • Payment history: every invoice (id, created, billing_reason, status,
    amount_due, amount_paid) and every charge (id, amount, paid, refunded,
    amount_refunded) — so you can see exactly what money moved
  • App DB (if reachable, read-only): email, created_at, plan, subscription_status,
    trial_used, subscribed_at, stripe_subscription
  • A computed classification + a RECOMMENDED ACTION (advisory only — nothing runs)

Groups:
  1 PREMIUM wrongly scheduled to cancel (H1)   2 trialing scheduled to cancel
  3 immediate-charge, no trial since launch (C1 candidate)
"""
import os
import sys
import json
import time
import sqlite3
import urllib.parse
import urllib.request
import urllib.error
import datetime

API = "https://api.stripe.com/v1"
KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
LAUNCH = (os.environ.get("TRIAL_LAUNCH_AT", "2026-11-03") or "").strip()
DB_PATH = os.environ.get("CALORIA_DB", "/data/caloria.db")

if not KEY:
    sys.exit("STRIPE_SECRET_KEY not set — aborting (no access).")


def api(path, params=None):
    """GET-only Stripe call. There is no code path here that can write."""
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {path} -> {e.code}: {e.read().decode('utf-8','ignore')[:300]}")


def ts(v):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(v)) if v else "—"


def launch_ts():
    s = LAUNCH.replace("T", " ").replace("Z", "").strip()[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(s, fmt).replace(
                tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            continue
    return 0


def iter_subs():
    after = None
    while True:
        p = {"status": "all", "limit": 100}
        if after:
            p["starting_after"] = after
        page = api("subscriptions", p)
        data = page.get("data", [])
        for s in data:
            yield s
        if not page.get("has_more") or not data:
            return
        after = data[-1]["id"]


def db_row(customer_id):
    """Read-only (mode=ro) lookup of the app's user row for a Stripe customer.
    Returns None if the DB isn't reachable (e.g. run off-Render) or no row."""
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute(
                "SELECT email, created_at, plan, subscription_status, trial_used, "
                "subscribed_at, stripe_subscription FROM users WHERE stripe_customer=?",
                (customer_id,),
            ).fetchone()
            return dict(r) if r else None
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 — DB cross-check is best-effort, never fatal
        print(f"    (app DB read-only lookup unavailable: {e})")
        return None


def classify(sub, groups, dbr, lt):
    """Return (labels, actions, flags) — advisory only. Evaluates ALL applicable
    groups (no short-circuit) and distinguishes a CUSTOMER/owner-requested cancel
    from a code-issued one, so a legitimate portal cancellation is not mislabeled
    as an H1 bug victim."""
    tier = ((sub.get("metadata") or {}).get("tier") or "").lower()
    cd = sub.get("cancellation_details") or {}
    labels, actions, flags = [], [], set()

    # Group 1 — scheduled to cancel while active.
    if 1 in groups:
        if tier == "starter":
            labels.append("Starter sub scheduled to cancel — expected by design")
            actions.append("No change.")
        elif cd.get("feedback") or cd.get("comment") or cd.get("reason") == "cancellation_requested":
            # Our code only EVER issues cancel_at_period_end for a subscription stamped
            # tier=starter. A premium sub with reason=cancellation_requested / feedback
            # was requested by the customer (portal) or owner (Dashboard) — NOT H1.
            labels.append(f"CUSTOMER/owner-REQUESTED cancel (reason={cd.get('reason')}, "
                          f"feedback={cd.get('feedback')}) — not caused by our code")
            actions.append("Do NOT un-cancel — honor the request (un-cancelling would re-charge at renewal).")
            flags.add("customer_cancel")
        else:
            labels.append("Premium scheduled to cancel, source UNCLEAR")
            actions.append("Verify via Stripe request logs / events before any action.")
            flags.add("verify_source")

    # Group 3 — immediate charge, no trial (evaluated INDEPENDENTLY of group 1).
    if 3 in groups:
        created_after = bool(dbr and dbr.get("created_at") and _norm(dbr["created_at"]) >= _norm(LAUNCH))
        never_trialed = bool(dbr and not dbr.get("trial_used"))
        if dbr and created_after and never_trialed:
            labels.append("LIKELY C1 VICTIM — eligible new account charged with NO trial")
            actions.append("Refund the first charge (or grant a 3-day credit). Fix C1 before re-offering the trial.")
            flags.add("c1_victim")
        elif dbr:
            labels.append("Immediate charge but account INELIGIBLE (pre-launch/returning) — correct")
            actions.append("No action.")
        else:
            labels.append("Immediate charge, eligibility UNVERIFIED (no DB row)")
            actions.append("Confirm eligibility from the app DB.")

    # Group 2 — trialing scheduled to cancel.
    if 2 in groups:
        labels.append("Trialing subscription scheduled to cancel at trial end")
        actions.append("repair_trials.py (dry-run first) un-schedules so the trial converts to paid.")

    if not labels:
        labels.append("(unclassified)")
        actions.append("Review manually.")
    return (" | ".join(labels), " | ".join(actions), flags)


def _norm(s):
    return str(s).replace("T", " ").replace("Z", "").strip()[:19]


def main():
    mode = "LIVE" if KEY.startswith("sk_live") else "TEST"
    lt = launch_ts()
    print(f"=== investigate_customers.py  mode={mode}  READ-ONLY  "
          f"(launch cutoff {LAUNCH})  db={DB_PATH} ===\n")

    # Collect affected subs, grouped, then dedup by customer.
    by_customer = {}   # cust_id -> {"subs": {sub_id: sub}, "groups": set()}
    scanned = 0
    for s in iter_subs():
        scanned += 1
        cust = s.get("customer")
        status = s.get("status")
        tier = ((s.get("metadata") or {}).get("tier") or "").lower()
        scheduled = bool(s.get("cancel_at_period_end") or s.get("cancel_at"))
        g = set()
        if scheduled and status == "active" and tier != "starter":
            g.add(1)
        if scheduled and status == "trialing":
            g.add(2)
        if (status == "active" and not s.get("trial_end") and tier != "starter"
                and (s.get("created") or 0) >= lt and lt > 0):
            g.add(3)
        if not g:
            continue
        rec = by_customer.setdefault(cust, {"subs": {}, "groups": set()})
        rec["subs"][s["id"]] = s
        rec["groups"] |= g

    print(f"subscriptions scanned: {scanned}   unique affected customers: {len(by_customer)}\n")
    print("=" * 74)

    refund_candidates, customer_cancels, verify_source, trialing = [], [], [], []
    for cust_id, rec in by_customer.items():
        cust = api(f"customers/{cust_id}")
        email = cust.get("email") or "(no email on Stripe)"
        dbr = db_row(cust_id)
        print(f"\nCUSTOMER {cust_id}")
        print(f"  email (Stripe) : {email}")
        if dbr:
            print(f"  email (app DB) : {dbr.get('email')}")
            print(f"  app DB         : created_at={dbr.get('created_at')}  plan={dbr.get('plan')}  "
                  f"status={dbr.get('subscription_status')}  trial_used={dbr.get('trial_used')}  "
                  f"subscribed_at={dbr.get('subscribed_at')}")
        print(f"  Stripe created : {ts(cust.get('created'))}   groups={sorted(rec['groups'])}")

        for sid, s in rec["subs"].items():
            price = (((s.get("items") or {}).get("data") or [{}])[0].get("price") or {})
            amt = price.get("unit_amount")
            print(f"  ── subscription {sid}")
            print(f"       status={s.get('status')}  tier={((s.get('metadata') or {}).get('tier') or '—')}  "
                  f"price={'$%.2f' % (amt/100) if amt else '—'}/{(price.get('recurring') or {}).get('interval','?')}")
            print(f"       created={ts(s.get('created'))}  trial={ts(s.get('trial_start'))} → {ts(s.get('trial_end'))}")
            print(f"       period={ts(s.get('current_period_start'))} → {ts(s.get('current_period_end'))}")
            print(f"       cancel_at_period_end={s.get('cancel_at_period_end')}  cancel_at={ts(s.get('cancel_at'))}  "
                  f"canceled_at={ts(s.get('canceled_at'))}")
            cd = s.get("cancellation_details") or {}
            print(f"       cancellation_reason={cd.get('reason')}  feedback={cd.get('feedback')}  "
                  f"comment={cd.get('comment')}")

        # payment history (per customer)
        print("  ── payment history (invoices)")
        inv = api("invoices", {"customer": cust_id, "limit": 100})
        for i in sorted(inv.get("data", []), key=lambda x: x.get("created") or 0):
            print(f"       {i.get('id')}  {ts(i.get('created'))}  reason={i.get('billing_reason')}  "
                  f"status={i.get('status')}  due=${(i.get('amount_due') or 0)/100:.2f}  "
                  f"paid=${(i.get('amount_paid') or 0)/100:.2f}")
        ch = api("charges", {"customer": cust_id, "limit": 100})
        if ch.get("data"):
            print("  ── charges / refunds")
            for c in sorted(ch.get("data", []), key=lambda x: x.get("created") or 0):
                print(f"       {c.get('id')}  {ts(c.get('created'))}  amount=${(c.get('amount') or 0)/100:.2f}  "
                      f"paid={c.get('paid')}  refunded={c.get('refunded')}  "
                      f"amount_refunded=${(c.get('amount_refunded') or 0)/100:.2f}")

        cls, action, flags = classify(list(rec["subs"].values())[0], rec["groups"], dbr, lt)
        print(f"  ►► CLASSIFICATION : {cls}")
        print(f"  ►► RECOMMENDED    : {action}")
        if "c1_victim" in flags:
            refund_candidates.append((cust_id, email))
        if "customer_cancel" in flags:
            customer_cancels.append((cust_id, email))
        if "verify_source" in flags:
            verify_source.append((cust_id, email))
        if 2 in rec["groups"]:
            trialing.append((cust_id, email))

    print("\n" + "=" * 74)
    print("=== SUMMARY (read-only; nothing changed) ===")
    print(f"unique affected customers                         : {len(by_customer)}")
    print(f"Customer/owner-REQUESTED cancels (NOT a bug)      : {len(customer_cancels)}  {customer_cancels}")
    print(f"Scheduled cancels with UNCLEAR source (verify)    : {len(verify_source)}  {verify_source}")
    print(f"Trialing scheduled to cancel                      : {len(trialing)}  {trialing}")
    print(f"Likely C1 refund/credit candidates                : {len(refund_candidates)}  {refund_candidates}")
    print("\nNo Stripe writes were made and the database was opened read-only.")


if __name__ == "__main__":
    main()
