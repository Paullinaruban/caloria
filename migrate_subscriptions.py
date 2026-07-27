#!/usr/bin/env python3
"""
migrate_subscriptions.py — move existing ACTIVE Stripe subscriptions onto a new
monthly Price (e.g. the new $15/month price).

WHY A SCRIPT: this mutates real customers' billing. It runs against LIVE Stripe
with YOUR secret key, DRY-RUN by default, and only changes things when you pass
--apply. It is idempotent (safe to re-run) and resumable.

WHAT IT DOES
  • Lists every subscription in the given statuses (default: active, trialing,
    past_due).
  • For each, looks at its single price. If that price is already the target, it
    skips. If the subscription's interval or currency doesn't match the target
    Price, it SKIPS and reports it (Stripe can't move a THB sub onto a USD-only
    price, or a yearly sub onto a monthly price).
  • Otherwise it swaps the subscription's item onto the new Price.

PRORATION (money behaviour) — choose with --proration:
  • none               (DEFAULT, recommended for a price DROP): no credit/charge
                        now; the customer simply renews at the new price next
                        cycle. Cleanest, no surprise invoices or credit balances.
  • create_prorations  : issues a prorated credit for the unused part of the old
                        price (applied to future invoices).
  • always_invoice     : prorates AND invoices immediately.

USAGE
  export STRIPE_SECRET_KEY=sk_live_...              # your LIVE key
  # 1) DRY RUN (no changes) — always do this first and read the summary:
  python3 migrate_subscriptions.py --new-price price_XXXX
  # 2) Apply for real:
  python3 migrate_subscriptions.py --new-price price_XXXX --apply
  # Options: --proration none|create_prorations|always_invoice
  #          --statuses active,trialing,past_due
  #          --include-yearly           (also migrate yearly subs — usually NO)
  #          --limit N                  (stop after N candidates — for a test run)
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.stripe.com/v1"


def _req(method, path, key, params=None, idempotency_key=None):
    url = f"{API}{path}"
    data = None
    if method == "GET" and params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    elif params:
        data = urllib.parse.urlencode(params, doseq=True).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if idempotency_key:
        req.add_header("Idempotency-Key", idempotency_key)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise SystemExit(f"[stripe {e.code}] {method} {path}\n{body}") from e


def get_target_price(key, price_id):
    # Expand currency_options so we know EVERY currency this Price supports (its
    # base currency plus any per-currency options). A subscription can only be
    # moved onto a Price that supports the subscription's own currency.
    p = _req("GET", f"/prices/{price_id}", key, params={"expand[]": "currency_options"})
    rec = p.get("recurring") or {}
    supported = {p["currency"]}
    supported.update((p.get("currency_options") or {}).keys())
    return {
        "id": p["id"],
        "currency": p["currency"],
        "supported": supported,           # every currency this Price can bill
        "interval": rec.get("interval"),
        "unit_amount": p.get("unit_amount"),
    }


def iter_subscriptions(key, statuses):
    """Yield every subscription across the requested statuses (paginated)."""
    for status in statuses:
        starting_after = None
        while True:
            params = {"status": status, "limit": 100, "expand[]": "data.items.data.price"}
            if starting_after:
                params["starting_after"] = starting_after
            page = _req("GET", "/subscriptions", key, params)
            for sub in page.get("data", []):
                yield sub
            if not page.get("has_more"):
                break
            starting_after = page["data"][-1]["id"]


def main():
    ap = argparse.ArgumentParser(description="Migrate Stripe subscriptions to a new Price.")
    ap.add_argument("--new-price", required=True, help="Target Price id (price_...)")
    ap.add_argument("--apply", action="store_true", help="Actually make changes (default is dry-run)")
    ap.add_argument("--proration", default="none",
                    choices=["none", "create_prorations", "always_invoice"])
    ap.add_argument("--statuses", default="active,trialing,past_due")
    ap.add_argument("--include-yearly", action="store_true",
                    help="Also migrate yearly subs (default: only same-interval as target)")
    ap.add_argument("--limit", type=int, default=0, help="Stop after N candidates (0 = all)")
    args = ap.parse_args()

    import os
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise SystemExit("Set STRIPE_SECRET_KEY (your LIVE key) in the environment first.")
    live = key.startswith("sk_live")
    mode = "LIVE" if live else "TEST"

    target = get_target_price(key, args.new_price)
    if not target["interval"]:
        raise SystemExit(f"{args.new_price} is not a recurring Price.")
    amount = (target["unit_amount"] or 0) / 100.0
    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]

    print("=" * 72)
    print(f"  Stripe subscription migration  [{mode} mode]  {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"  Target price : {target['id']}  ({amount:.2f} {target['currency'].upper()} / {target['interval']})")
    print(f"  Proration    : {args.proration}")
    print(f"  Statuses     : {', '.join(statuses)}")
    print("=" * 72)

    migrated = skipped_same = skipped_currency = skipped_interval = skipped_multi = errors = 0
    candidates = 0

    for sub in iter_subscriptions(key, statuses):
        items = (sub.get("items") or {}).get("data") or []
        sub_id = sub["id"]
        cust = sub.get("customer")
        if len(items) != 1:
            skipped_multi += 1
            print(f"  SKIP  {sub_id} (cust {cust}): has {len(items)} items — review by hand")
            continue
        item = items[0]
        price = item.get("price") or {}
        cur_price_id = price.get("id")
        cur_interval = (price.get("recurring") or {}).get("interval")
        cur_currency = price.get("currency")

        if cur_price_id == target["id"]:
            skipped_same += 1
            continue  # already on the target price
        if cur_interval != target["interval"] and not (args.include_yearly and cur_interval == "year"):
            skipped_interval += 1
            print(f"  SKIP  {sub_id}: interval {cur_interval} != target {target['interval']}")
            continue
        if cur_currency not in target["supported"]:
            skipped_currency += 1
            print(f"  SKIP  {sub_id}: currency {cur_currency} not supported by target Price "
                  f"(supports {sorted(target['supported'])} — add a currency_option for {cur_currency})")
            continue

        candidates += 1
        label = f"{sub_id} (cust {cust}) {cur_price_id} -> {target['id']}"
        if not args.apply:
            print(f"  WOULD MIGRATE  {label}")
        else:
            try:
                _req("POST", f"/subscriptions/{sub_id}", key, params={
                    "items[0][id]": item["id"],
                    "items[0][price]": target["id"],
                    "proration_behavior": args.proration,
                    # keep their renewal date; don't reset the billing cycle
                    "billing_cycle_anchor": "unchanged",
                }, idempotency_key=f"migrate_{sub_id}_{target['id']}")
                migrated += 1
                print(f"  MIGRATED       {label}")
                time.sleep(0.15)  # be gentle on rate limits
            except SystemExit as e:
                errors += 1
                print(f"  ERROR          {label}\n{e}")

        if args.limit and candidates >= args.limit:
            print(f"  (stopping after --limit {args.limit})")
            break

    print("-" * 72)
    print(f"  candidates             : {candidates}")
    print(f"  migrated               : {migrated}" + ("" if args.apply else "  (dry run — nothing changed)"))
    print(f"  skipped (already $new) : {skipped_same}")
    print(f"  skipped (interval)     : {skipped_interval}")
    print(f"  skipped (currency)     : {skipped_currency}")
    print(f"  skipped (multi-item)   : {skipped_multi}")
    print(f"  errors                 : {errors}")
    print("=" * 72)
    if not args.apply and candidates:
        print("  This was a DRY RUN. Re-run with --apply to make these changes.")


if __name__ == "__main__":
    main()
