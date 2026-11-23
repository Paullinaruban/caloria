#!/usr/bin/env python3
"""READ-ONLY live-Stripe audit of subscriptions affected by the billing bugs.

Makes ONLY GET requests — it never modifies, cancels, charges, or deletes
anything. Run it in the Render Shell (where STRIPE_SECRET_KEY is already set) to
get the list of customers to review before any repair. Pure stdlib.

    python3 audit_customers.py

It flags three groups:
  1) PREMIUM-WRONGLY-SCHEDULED-TO-CANCEL (H1 victims): status active, a scheduled
     cancellation (cancel_at_period_end / cancel_at), and the subscription is NOT a
     Starter (metadata.tier != 'starter'). These are Premium subs that should keep
     renewing — candidates to un-schedule (with your approval).
  2) TRIALING-SCHEDULED-TO-CANCEL: trialing + scheduled cancellation. `repair_trials.py`
     already fixes these safely (dry-run first).
  3) IMMEDIATE-CHARGE-NO-TRIAL (C1 victims): status active, never had a trial
     (trial_end is null), created on/after TRIAL_LAUNCH_AT, and NOT a Starter —
     i.e. a new customer who was charged $19.99 immediately instead of getting the
     3-day trial. Refund / trial-credit candidates (your decision).

Nothing here changes Stripe or the database.
"""
import os
import sys
import json
import time
import urllib.parse
import urllib.request
import urllib.error
import datetime

API = "https://api.stripe.com/v1"
KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
LAUNCH = (os.environ.get("TRIAL_LAUNCH_AT", "2026-11-03") or "").strip()

if not KEY:
    sys.exit("STRIPE_SECRET_KEY not set — aborting (no access).")


def api(path, params=None):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {path} -> {e.code}: {e.read().decode('utf-8','ignore')[:300]}")


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


def main():
    mode = "LIVE" if KEY.startswith("sk_live") else "TEST"
    lt = launch_ts()
    print(f"=== audit_customers.py  mode={mode}  READ-ONLY  "
          f"(trial launch cutoff: {LAUNCH} = {lt}) ===")

    premium_scheduled, trialing_scheduled, immediate_no_trial = [], [], []
    total = 0
    for s in iter_subs():
        total += 1
        sid = s["id"]
        cust = s.get("customer")
        status = s.get("status")
        tier = ((s.get("metadata") or {}).get("tier") or "").lower()
        scheduled = bool(s.get("cancel_at_period_end") or s.get("cancel_at"))
        trial_end = s.get("trial_end")
        created = s.get("created") or 0
        is_starter = (tier == "starter")

        if scheduled and status == "active" and not is_starter:
            premium_scheduled.append((sid, cust, s.get("cancel_at_period_end"),
                                      s.get("cancel_at"), tier or "(none)"))
        if scheduled and status == "trialing":
            trialing_scheduled.append((sid, cust, trial_end))
        if (status == "active" and not trial_end and not is_starter
                and created >= lt and lt > 0):
            immediate_no_trial.append((sid, cust, created, tier or "(none)"))

    def dump(title, rows, fmt):
        print(f"\n--- {title}: {len(rows)} ---")
        for r in rows:
            print("   " + fmt(r))

    dump("1) PREMIUM WRONGLY SCHEDULED TO CANCEL (H1 — review/un-schedule w/ approval)",
         premium_scheduled,
         lambda r: f"{r[0]} cust={r[1]} cancel_at_period_end={r[2]} cancel_at={r[3]} tier={r[4]}")
    dump("2) TRIALING SCHEDULED TO CANCEL (repair_trials.py handles; dry-run first)",
         trialing_scheduled,
         lambda r: f"{r[0]} cust={r[1]} trial_end={r[2]}")
    dump("3) IMMEDIATE-CHARGE, NO TRIAL — created after launch (C1 refund/credit candidates)",
         immediate_no_trial,
         lambda r: f"{r[0]} cust={r[1]} created={r[2]} ({time.strftime('%Y-%m-%d', time.gmtime(r[2]))}) tier={r[3]}")

    print("\n=== SUMMARY ===")
    print(f"subscriptions scanned                         : {total}")
    print(f"Premium wrongly scheduled to cancel (H1)      : {len(premium_scheduled)}")
    print(f"Trialing scheduled to cancel                  : {len(trialing_scheduled)}")
    print(f"Immediate-charge-no-trial since launch (C1)   : {len(immediate_no_trial)}")
    print("\nNOTHING was modified. Decide remediation (refunds/credits/un-scheduling) separately.")


if __name__ == "__main__":
    main()
