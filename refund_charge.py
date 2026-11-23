#!/usr/bin/env python3
"""Refund ONE Stripe charge — DRY-RUN by default, with strict safety rails.

This is the only script in this project that can issue a refund, and it does so
ONLY when you pass --apply. By itself (no flag) it changes nothing: it just shows
you exactly what it would refund. It NEVER touches the subscription (no cancel, no
reactivate, no un-schedule) and refunds at most ONE charge per run.

Intended use (Elena's $19.99 C1 charge):
    # 1) DRY RUN — look first, change nothing:
    python3 refund_charge.py --customer cus_XXXX
    # or target the exact charge:
    python3 refund_charge.py --charge ch_XXXX
    # 2) Only after you approve, actually issue the refund:
    python3 refund_charge.py --charge ch_XXXX --apply

Safety:
  * DRY-RUN unless --apply is given.
  * Refunds the FULL charge amount by default (or --amount <cents> for partial).
  * Refuses if the charge is not paid, is already refunded, or is disputed.
  * Uses a deterministic Idempotency-Key, so an accidental re-run never double-refunds.
  * Re-reads the charge afterward and only reports success if Stripe confirms it.
  * Makes NO subscription/customer writes whatsoever.
Reads STRIPE_SECRET_KEY from the environment; never prints it. Pure stdlib.
"""
import os
import sys
import json
import argparse
import urllib.parse
import urllib.request
import urllib.error

API = "https://api.stripe.com/v1"
KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
if not KEY:
    sys.exit("STRIPE_SECRET_KEY not set — aborting.")


def api(path, params=None, method="GET", idempotency_key=None):
    url = f"{API}/{path}"
    headers = {"Authorization": f"Bearer {KEY}"}
    data = None
    if method == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(params or {}, doseq=True).encode()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode('utf-8','ignore')[:300]}")


def pick_charge(customer_id):
    """Return the single refundable paid charge for a customer, or exit if 0 / >1."""
    page = api("charges", {"customer": customer_id, "limit": 100})
    paid = [c for c in page.get("data", [])
            if c.get("paid") and not c.get("refunded") and (c.get("amount") or 0) > 0
            and c.get("status") == "succeeded"]
    if not paid:
        sys.exit(f"No refundable (paid, not-yet-refunded) charge found for {customer_id}.")
    if len(paid) > 1:
        ids = ", ".join(c["id"] for c in paid)
        sys.exit(f"{len(paid)} refundable charges for {customer_id}: {ids}\n"
                 f"Re-run with --charge <id> to name exactly which one.")
    return paid[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--charge", help="Charge id (ch_...) to refund")
    ap.add_argument("--customer", help="Customer id (cus_...) — auto-pick its single paid charge")
    ap.add_argument("--amount", type=int, help="Partial amount in CENTS (default: full charge)")
    ap.add_argument("--apply", action="store_true", help="Actually issue the refund (omit = dry run)")
    args = ap.parse_args()
    if not (args.charge or args.customer):
        sys.exit("Pass --charge ch_... or --customer cus_...")

    mode = "LIVE" if KEY.startswith("sk_live") else "TEST"
    ch = api(f"charges/{args.charge}") if args.charge else pick_charge(args.customer)
    amount = ch.get("amount") or 0
    refund_amount = args.amount if args.amount else amount

    print(f"=== refund_charge.py  mode={mode}  {'APPLY' if args.apply else 'DRY-RUN'} ===")
    print(f"  charge      : {ch.get('id')}")
    print(f"  customer    : {ch.get('customer')}")
    print(f"  invoice     : {ch.get('invoice')}")
    print(f"  amount      : ${amount/100:.2f} {str(ch.get('currency','')).upper()}")
    print(f"  paid        : {ch.get('paid')}   refunded: {ch.get('refunded')}   "
          f"disputed: {ch.get('disputed')}   status: {ch.get('status')}")
    print(f"  description : {ch.get('description')}")
    print(f"  → would refund: ${refund_amount/100:.2f}  (reason=requested_by_customer)")
    print("  NOTE: this does NOT touch the subscription — it stays exactly as the customer left it.")

    # Safety gates.
    if not ch.get("paid") or ch.get("status") != "succeeded":
        sys.exit("ABORT: charge is not a succeeded/paid charge.")
    if ch.get("refunded") or (ch.get("amount_refunded") or 0) >= amount:
        sys.exit("ABORT: charge is already fully refunded.")
    if ch.get("disputed"):
        sys.exit("ABORT: charge is disputed — handle the dispute in Stripe instead of refunding.")
    if refund_amount <= 0 or refund_amount > amount:
        sys.exit(f"ABORT: refund amount ${refund_amount/100:.2f} is invalid for a ${amount/100:.2f} charge.")

    if not args.apply:
        print("\nDRY RUN — nothing was refunded. Re-run with --apply to issue it.")
        return

    params = {"charge": ch["id"], "amount": refund_amount, "reason": "requested_by_customer"}
    refund = api("refunds", params, method="POST", idempotency_key=f"refund_{ch['id']}")
    after = api(f"charges/{ch['id']}")
    ok = (refund.get("status") in ("succeeded", "pending")
          and (after.get("amount_refunded") or 0) >= refund_amount)
    print(f"\n  refund id   : {refund.get('id')}   status: {refund.get('status')}")
    print(f"  charge now  : refunded={after.get('refunded')}  "
          f"amount_refunded=${(after.get('amount_refunded') or 0)/100:.2f}")
    print("REFUND CONFIRMED." if ok else "REFUND NOT CONFIRMED — check Stripe dashboard.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
