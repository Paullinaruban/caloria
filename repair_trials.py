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
      - remove the scheduled cancellation (see below)
      - default_payment_method -> the customer's card, ONLY if the sub had none
      - collection_method      -> charge_automatically (only if it wasn't already)
    It never touches trial_end, items, or price. It NEVER charges, NEVER creates
    or deletes a subscription.
  * Stripe rejects cancel_at_period_end and cancel_at in the SAME request, so we
    send only ONE cancellation parameter per request:
      - a hard cancel_at date  -> clear it with  cancel_at=""   (one request)
      - cancel_at_period_end   -> set            cancel_at_period_end=false
      - if (rare) both are set -> clear cancel_at first, then unset the flag in a
        SECOND request.
  * After each repair we RE-RETRIEVE the subscription and only count it repaired
    if Stripe confirms: no scheduled cancellation, trial_end unchanged, and a
    default payment method is present.
  * One subscription failing does not stop the others.

USAGE (run in the Render Shell; STRIPE_SECRET_KEY is already set there):
    python3 repair_trials.py            # DRY RUN — inspect only
    python3 repair_trials.py --apply    # perform the safe repairs
"""
import os, sys, json, urllib.parse, urllib.request, urllib.error

API = "https://api.stripe.com/v1"


class StripeError(RuntimeError):
    pass


def _cust_pm(sub):
    """Customer-level default payment method, when the customer is expanded."""
    cust = sub.get("customer")
    if isinstance(cust, dict):
        return (cust.get("invoice_settings") or {}).get("default_payment_method")
    return None


def plan_updates(sub):
    """Pure: return an ORDERED list of conflict-free Stripe update param dicts.

    Guarantees no single dict contains BOTH cancel_at_period_end and cancel_at.
    Returns [] when there is nothing to change.
    """
    pm_extra = {}
    if (not sub.get("default_payment_method")) and _cust_pm(sub):
        pm_extra["default_payment_method"] = _cust_pm(sub)
    if sub.get("collection_method") not in (None, "charge_automatically"):
        pm_extra["collection_method"] = "charge_automatically"

    has_flag = bool(sub.get("cancel_at_period_end"))
    has_date = bool(sub.get("cancel_at"))
    updates = []
    if has_date:
        updates.append({**pm_extra, "cancel_at": ""})      # clear the hard date
        if has_flag:
            updates.append({"cancel_at_period_end": "false"})  # separate request
    elif has_flag:
        updates.append({**pm_extra, "cancel_at_period_end": "false"})
    elif pm_extra:
        updates.append(dict(pm_extra))
    # Invariant: never both cancellation params in one request.
    for u in updates:
        assert not ("cancel_at" in u and "cancel_at_period_end" in u), u
    return updates


def verify(before, after):
    """Return (ok, reasons) confirming Stripe actually removed the cancellation,
    kept trial_end, and has a usable payment method."""
    reasons = []
    if after.get("cancel_at_period_end"):
        reasons.append("cancel_at_period_end still true")
    if after.get("cancel_at"):
        reasons.append("cancel_at still set")
    if after.get("trial_end") != before.get("trial_end"):
        reasons.append(f"trial_end changed {before.get('trial_end')}->{after.get('trial_end')}")
    if not (after.get("default_payment_method") or _cust_pm(after)):
        reasons.append("no default payment method after update")
    return (not reasons), reasons


# ----------------------------------------------------------------------------
# Everything below talks to Stripe; the logic above is pure and unit-tested.
# ----------------------------------------------------------------------------
def _make_api(key):
    def api(path, params=None, method="GET"):
        url = f"{API}/{path}"
        data, headers = None, {"Authorization": f"Bearer {key}"}
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
            raise StripeError(f"{e.code}: {e.read().decode('utf-8','ignore')[:300]}")
        except urllib.error.URLError as e:
            raise StripeError(f"network: {e.reason}")
    return api


def iter_trialing(api):
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


def _cust_id(sub):
    c = sub.get("customer")
    return c.get("id") if isinstance(c, dict) else c


def main():
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        sys.exit("STRIPE_SECRET_KEY not set in this environment — aborting (no access).")
    apply = "--apply" in sys.argv
    api = _make_api(key)
    mode = "LIVE" if key.startswith("sk_live") else "TEST"
    print(f"=== repair_trials.py  mode={mode}  {'APPLY' if apply else 'DRY-RUN'} ===")

    inspected = scheduled = repaired = 0
    missing_pm, failed = [], []
    for s in iter_trialing(api):
        inspected += 1
        if s.get("status") != "trialing":
            continue
        if not (s.get("cancel_at_period_end") or s.get("cancel_at")):
            continue
        scheduled += 1
        sid = s["id"]
        usable = bool(s.get("default_payment_method") or _cust_pm(s))
        print(f"- {sid} cust={_cust_id(s)} trial_end={s.get('trial_end')} "
              f"cancel_at_period_end={s.get('cancel_at_period_end')} cancel_at={s.get('cancel_at')} "
              f"sub_pm={'yes' if s.get('default_payment_method') else 'no'} "
              f"cust_pm={'yes' if _cust_pm(s) else 'no'} collection={s.get('collection_method')}"
              f"{'' if usable else '   [NO USABLE CARD]'}")
        if not usable:
            missing_pm.append(sid)
            print("    SKIP — no usable payment method (cannot be charged off-session)")
            continue

        updates = plan_updates(s)
        if not apply:
            print(f"    WOULD send {len(updates)} update request(s): "
                  + " | ".join(json.dumps(u) for u in updates) + "  (dry run)")
            continue

        try:
            for u in updates:
                api(f"subscriptions/{sid}", u, "POST")
            after = api(f"subscriptions/{sid}", {"expand[]": "customer"}, "GET")
            ok, reasons = verify(s, after)
            if ok:
                repaired += 1
                print(f"    REPAIRED (Stripe-confirmed): status={after.get('status')} "
                      f"cancel_at_period_end={after.get('cancel_at_period_end')} "
                      f"cancel_at={after.get('cancel_at')} trial_end={after.get('trial_end')} "
                      f"default_pm={'yes' if after.get('default_payment_method') else 'no'} "
                      f"collection={after.get('collection_method')}")
            else:
                failed.append((sid, "; ".join(reasons)))
                print(f"    NOT CONFIRMED: {'; '.join(reasons)}")
        except StripeError as e:
            failed.append((sid, str(e)))
            print(f"    ERROR (skipped, continuing): {e}")

    print("\n=== SUMMARY ===")
    print(f"trialing subscriptions inspected       : {inspected}")
    print(f"scheduled to cancel at trial end       : {scheduled}")
    print(f"repaired (Stripe-confirmed)            : {repaired if apply else 0}"
          f"{'' if apply else '  (dry run — rerun with --apply)'}")
    print(f"missing usable payment method (skipped): {len(missing_pm)}  {missing_pm if missing_pm else ''}")
    print(f"failed / not confirmed                 : {len(failed)}  {failed if failed else ''}")
    print("No immediate charges were triggered (removing a scheduled cancellation never charges).")


if __name__ == "__main__":
    main()
