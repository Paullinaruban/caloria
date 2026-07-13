#!/usr/bin/env python3
"""
Caloria — READ-ONLY Stripe diagnostics for international payment approval.

Run this in the Render Shell of your PRODUCTION backend (STRIPE_SECRET_KEY is
already in the environment there). It makes ONLY GET requests — it creates,
changes, and charges NOTHING. It prints the facts needed to find why
international cards are declined:

  • account country + default (settlement) currency + capabilities
  • your settlement currencies (from balance)
  • recent Checkout Sessions: the currency they were actually created in, and
    whether Adaptive Pricing converted them (currency_conversion present?)
  • recent charges/PaymentIntents: real decline codes + the buyer's card country
  • whether any Customer is currency-locked to USD

USAGE (Render Shell):
    python stripe_diagnose.py
"""
import os, json, urllib.request, urllib.error, urllib.parse

KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
API = "https://api.stripe.com/v1"


def get(path, params=None):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"__error__": f"HTTP {e.code}: {e.read().decode('utf-8','ignore')[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"__error__": str(e)}


def line(c="-"):
    print(c * 64)


def main():
    if not KEY:
        print("ERROR: STRIPE_SECRET_KEY not set in this environment."); return
    print(f"Key mode: {'LIVE' if KEY.startswith('sk_live') else 'TEST'}  ({KEY[:8]}…)")
    line("=")

    # 1) Account: country, settlement currency, capabilities
    acct = get("account")
    if "__error__" in acct:
        print("account:", acct["__error__"])
    else:
        print("ACCOUNT")
        print("  country          :", acct.get("country"))
        print("  default_currency :", acct.get("default_currency"), "  <-- your settlement currency")
        caps = acct.get("capabilities", {})
        print("  card_payments    :", caps.get("card_payments"))
        print("  transfers        :", caps.get("transfers"))
        # any capability not 'active' is worth noting
        inactive = {k: v for k, v in caps.items() if v != "active"}
        if inactive:
            print("  NON-ACTIVE caps  :", inactive)
    line()

    # 2) Settlement currencies actually held
    bal = get("balance")
    if "__error__" not in bal:
        avail = {b["currency"] for b in bal.get("available", [])} | {b["currency"] for b in bal.get("pending", [])}
        print("SETTLEMENT CURRENCIES (from balance):", ", ".join(sorted(avail)) or "(none yet)")
    line()

    # 3) Recent Checkout Sessions — the smoking gun for Adaptive Pricing
    print("RECENT CHECKOUT SESSIONS (currency actually used + Adaptive Pricing?)")
    ses = get("checkout/sessions", {"limit": 15})
    conv_yes = conv_no = 0
    for s in ses.get("data", []):
        cc = s.get("currency_conversion")
        converted = "YES" if cc else "no"
        if cc: conv_yes += 1
        else: conv_no += 1
        cust_country = ((s.get("customer_details") or {}).get("address") or {}).get("country")
        print(f"  {s.get('id')[:20]:22} status={str(s.get('status')):9} "
              f"currency={str(s.get('currency')).upper():4} adaptive_converted={converted:3} "
              f"buyer_country={cust_country or '?'}")
    print(f"  -> sessions WITH conversion: {conv_yes}   WITHOUT: {conv_no}")
    print("     (If buyers are abroad but currency=USD and adaptive_converted=no,")
    print("      Adaptive Pricing is NOT applying — usually the settlement-currency rule.)")
    line()

    # 4) Recent charges — real decline codes + card country
    print("RECENT CHARGES (decline codes + card country)")
    ch = get("charges", {"limit": 25})
    declines = {}
    for c in ch.get("data", []):
        card = (c.get("payment_method_details") or {}).get("card") or {}
        country = card.get("country")
        brand = card.get("brand")
        if c.get("paid"):
            print(f"  {c.get('id')[:20]:22} PAID   {str(c.get('currency')).upper():4} "
                  f"{str(brand):10} card_country={country}")
        else:
            code = c.get("failure_code") or ((c.get("outcome") or {}).get("reason"))
            msg = (c.get("outcome") or {}).get("seller_message") or c.get("failure_message")
            declines[code] = declines.get(code, 0) + 1
            print(f"  {c.get('id')[:20]:22} FAIL   {str(c.get('currency')).upper():4} "
                  f"{str(brand):10} card_country={country} code={code}")
            if msg: print(f"       -> {msg[:80]}")
    if declines:
        print("  DECLINE CODE TOTALS:", dict(sorted(declines.items(), key=lambda x: -x[1])))
    line()

    # 5) PER-PAYMENT breakdown — one row per recent PaymentIntent, every field
    print("PER-PAYMENT BREAKDOWN (last 25 PaymentIntents — the full table)")
    pis = get("payment_intents", {"limit": 25, "expand[]": "data.latest_charge"})
    if "__error__" in pis:
        print("  payment_intents:", pis["__error__"])
    else:
        for p in pis.get("data", []):
            ch = p.get("latest_charge") or {}
            card = (ch.get("payment_method_details") or {}).get("card") or {}
            oc = ch.get("outcome") or {}
            lpe = p.get("last_payment_error") or {}
            tds = (card.get("three_d_secure") or {})
            pm_types = ",".join(p.get("payment_method_types") or [])
            print(f"  PI {p.get('id')[:20]}")
            print(f"     status={p.get('status')}  currency={str(p.get('currency')).upper()}  amount={p.get('amount')}")
            print(f"     charge_status={ch.get('status')}  paid={ch.get('paid')}  captured={ch.get('captured')}")
            print(f"     decline_code={lpe.get('decline_code') or ch.get('failure_code')}  "
                  f"error_code={lpe.get('code')}  reason={oc.get('reason')}")
            print(f"     issuer_msg={oc.get('seller_message')}  network_status={oc.get('network_status')}  "
                  f"risk={oc.get('risk_level')}({oc.get('risk_score')})  outcome_type={oc.get('type')}")
            print(f"     pm_types={pm_types}  card_brand={card.get('brand')}  card_country={card.get('country')}  "
                  f"funding={card.get('funding')}")
            print(f"     3ds_required={bool(tds)}  3ds_result={tds.get('result')}  "
                  f"card_wallet={(card.get('wallet') or {}).get('type')}")
    line()

    # 6) Customer currency lock check (first 20)
    print("CUSTOMER CURRENCY LOCK (a customer pinned to a currency can't change it)")
    cus = get("customers", {"limit": 20})
    locked = {}
    for c in cus.get("data", []):
        cur = c.get("currency")
        if cur:
            locked[cur] = locked.get(cur, 0) + 1
    print("  currencies locked on existing customers:", locked or "(none — customers not yet currency-locked)")
    line("=")
    print("Done. Share this output and I'll pinpoint the exact cause.")


if __name__ == "__main__":
    main()
