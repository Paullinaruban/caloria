#!/usr/bin/env python3
"""Inspect and repair trial subscriptions wrongly scheduled to cancel at trial end.

Reads STRIPE_SECRET_KEY from the environment (e.g. the Render Shell). It is never
printed. Pure Python stdlib — no dependencies.

SAFETY
  * DRY-RUN by default: prints findings, changes NOTHING.
  * Only considers subscriptions whose status is exactly 'trialing'.
  * "Scheduled to cancel" = cancel_at_period_end is true OR cancel_at is set.
  * "Usable card" = the subscription has a default_payment_method OR the customer
    has invoice_settings.default_payment_method (Stripe falls back to it).
  * Repair (--apply) only for trialing + scheduled + usable-card subs, and only:
      - cancel_at_period_end -> false   (remove the unintended cancellation)
      - cancel_at            -> cleared  (if it was set)
      - default_payment_method -> the customer's card, ONLY if the sub had none
      - collection_method    -> charge_automatically (only if it wasn't already)
    It never touches trial_end, items, or price. It NEVER charges, NEVER creates
    or deletes a subscription.
  * Subs with NO usable card are reported separately and NOT repaired.

USAGE (run in the Render Shell; STRIPE_SECRET_KEY is already set there):
    python3 repair_trials.py            # DRY RUN — inspect only
    python3 repair_trials.py --apply    # perform the safe repairs
"""
import os, sys, json, urllib.parse, urllib.request, urllib.error

API = "https://api.stripe.com/v1"
KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
APPLY = "--apply" in sys.argv

if not KEY:
    sys.exit("STRIPE_SECRET_KEY not set in this environment — aborting (no access).")
MODE = "LIVE" if KEY.startswith("sk_live") else "TEST"


def api(path, params=None, method="GET"):
    url = f"{API}/{path}"
    data, headers = None, {"Authorization": f"Bearer {KEY}"}
    if method == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(params or {}, doseq=True).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Stripe API error {e.code}: {e.read().decode('utf-8','ignore')[:300]}")


def iter_trialing():
    after = None
    while True:
        p = {"status": "trialing", "limit": 100, "expand[]": "data.customer"}
        if after:
            p["starting_after"] = after
        page = api("subscriptions", p, "GET")
        for s in page.get("data", []):
            yield s
        if not page.get("has_more"):
            return
        after = page["data"][-1]["id"]


def cust_default_pm(sub):
    cust = sub.get("customer")
    if isinstance(cust, dict):
        return (cust.get("invoice_settings") or {}).get("default_payment_method")
    return None


def main():
    print(f"=== repair_trials.py  mode={MODE}  {'APPLY' if APPLY else 'DRY-RUN'} ===")
    inspected = scheduled = repaired = 0
    missing_pm = []
    for s in iter_trialing():
        inspected += 1
        if s.get("status") != "trialing":
            continue
        if not (s.get("cancel_at_period_end") or s.get("cancel_at")):
            continue
        scheduled += 1
        sid = s["id"]
        sub_pm = s.get("default_payment_method")
        c_pm = cust_default_pm(s)
        usable = bool(sub_pm or c_pm)
        cm = s.get("collection_method")
        tag = "" if usable else "   [NO USABLE CARD]"
        print(f"- {sid} cust={s.get('customer',{}).get('id') if isinstance(s.get('customer'),dict) else s.get('customer')} "
              f"trial_end={s.get('trial_end')} cancel_at_period_end={s.get('cancel_at_period_end')} "
              f"cancel_at={s.get('cancel_at')} sub_pm={'yes' if sub_pm else 'no'} "
              f"cust_pm={'yes' if c_pm else 'no'} collection={cm}{tag}")
        if not usable:
            missing_pm.append(sid)
            print("    SKIP — no usable payment method (cannot be charged off-session)")
            continue
        if not APPLY:
            extra = []
            if not sub_pm and c_pm:
                extra.append("set sub default_payment_method from customer")
            if cm != "charge_automatically":
                extra.append("set collection_method=charge_automatically")
            print(f"    WOULD: remove scheduled cancellation"
                  + (f"; also {'; '.join(extra)}" if extra else "") + " (dry run)")
            continue
        upd = {"cancel_at_period_end": "false"}
        if s.get("cancel_at"):
            upd["cancel_at"] = ""
        if not sub_pm and c_pm:
            upd["default_payment_method"] = c_pm
        if cm != "charge_automatically":
            upd["collection_method"] = "charge_automatically"
        res = api(f"subscriptions/{sid}", upd, "POST")
        ok = (not res.get("cancel_at_period_end")) and (not res.get("cancel_at"))
        print(f"    {'REPAIRED' if ok else 'POSTED (verify)'}: status={res.get('status')} "
              f"cancel_at_period_end={res.get('cancel_at_period_end')} "
              f"default_pm={'yes' if res.get('default_payment_method') else 'no'} "
              f"collection={res.get('collection_method')} trial_end={res.get('trial_end')}")
        if ok:
            repaired += 1

    print("\n=== SUMMARY ===")
    print(f"trialing subscriptions inspected       : {inspected}")
    print(f"scheduled to cancel at trial end       : {scheduled}")
    print(f"repaired                               : {repaired if APPLY else 0}"
          f"{'' if APPLY else '  (dry run — rerun with --apply)'}")
    print(f"missing usable payment method (skipped): {len(missing_pm)}  {missing_pm if missing_pm else ''}")
    print("No immediate charges were triggered (removing a scheduled cancellation never charges).")


if __name__ == "__main__":
    main()
