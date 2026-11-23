#!/usr/bin/env python3
"""READ-ONLY forensics for a single subscription's cancellation.

Makes ONLY Stripe GET requests. It answers: WHEN was cancel_at_period_end set,
and by WHAT request — so you can tell a customer portal cancel from a Dashboard
or API one. It changes nothing. Pure stdlib.

    python3 cancel_forensics.py --subscription sub_XXXX

What it shows:
  • The subscription's cancellation_details (reason / feedback / comment),
    canceled_at, cancel_at, cancel_at_period_end, status, created, metadata.tier.
    (A populated `feedback` almost always means the CUSTOMER used the portal.)
  • The customer.subscription.updated EVENT whose previous_attributes include
    cancel_at_period_end — i.e. the exact event that FLIPPED it — with its
    created time and its `request` {id, idempotency_key}.
      - request.idempotency_key starting 'trialblock'/'checkout'/'price'/'product'
        would indicate OUR backend. Our code sets NO idempotency key on a cancel,
        and only ever cancels Starter subs — so a Premium sub here should NOT trace
        to our code.
      - To label portal vs Dashboard vs API authoritatively, copy the printed
        request id (req_...) into Stripe → Developers → Logs (it shows the source).
"""
import os
import sys
import json
import time
import argparse
import urllib.parse
import urllib.request
import urllib.error

API = "https://api.stripe.com/v1"
KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
if not KEY:
    sys.exit("STRIPE_SECRET_KEY not set — aborting.")


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


def ts(v):
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(v)) if v else "—"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subscription", required=True, help="sub_... to investigate")
    args = ap.parse_args()
    sid = args.subscription
    mode = "LIVE" if KEY.startswith("sk_live") else "TEST"
    print(f"=== cancel_forensics.py  mode={mode}  READ-ONLY  subscription={sid} ===\n")

    s = api(f"subscriptions/{sid}")
    cd = s.get("cancellation_details") or {}
    print("SUBSCRIPTION STATE")
    print(f"  status                 : {s.get('status')}")
    print(f"  tier (metadata)        : {(s.get('metadata') or {}).get('tier')}")
    print(f"  created                : {ts(s.get('created'))}")
    print(f"  current_period_end     : {ts(s.get('current_period_end'))}")
    print(f"  cancel_at_period_end   : {s.get('cancel_at_period_end')}")
    print(f"  cancel_at              : {ts(s.get('cancel_at'))}")
    print(f"  canceled_at            : {ts(s.get('canceled_at'))}")
    print(f"  cancellation reason    : {cd.get('reason')}")
    print(f"  cancellation feedback  : {cd.get('feedback')}   (set → customer used the portal)")
    print(f"  cancellation comment   : {cd.get('comment')}")

    print("\nEVENT HISTORY — searching for the event that flipped cancel_at_period_end")
    print("(Stripe retains events ~30 days; older cancellations may not appear.)")
    found = False
    after = None
    scanned = 0
    while True:
        p = {"type": "customer.subscription.updated", "limit": 100}
        if after:
            p["starting_after"] = after
        page = api("events", p)
        data = page.get("data", [])
        for ev in data:
            scanned += 1
            obj = ev.get("data", {}).get("object", {})
            if obj.get("id") != sid:
                continue
            prev = ev.get("data", {}).get("previous_attributes", {}) or {}
            if "cancel_at_period_end" in prev or "cancel_at" in prev:
                req = ev.get("request") or {}
                print(f"  ► {ev.get('id')}  at {ts(ev.get('created'))}")
                print(f"      cancel_at_period_end: {prev.get('cancel_at_period_end')} → {obj.get('cancel_at_period_end')}")
                print(f"      request.id            : {req.get('id')}")
                print(f"      request.idempotency_key: {req.get('idempotency_key')}")
                print(f"      → copy request.id into Stripe → Developers → Logs to see the SOURCE")
                found = True
        if not page.get("has_more") or not data:
            break
        after = data[-1]["id"]
    if not found:
        print(f"  (no cancel-flipping event found in the last ~30 days; scanned {scanned} updated events)")
        print("  Check Stripe → Developers → Logs filtered to this subscription id, and the")
        print("  subscription's timeline on its Dashboard page, for the cancellation source.")

    print("\nREAD-ONLY — no Stripe writes were made.")


if __name__ == "__main__":
    main()
