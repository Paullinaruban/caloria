#!/usr/bin/env python3
"""
fix_email_dns.py — end-to-end fix for Resend email verification on a domain whose
DNS is managed by NETLIFY DNS (backed by NS1). It:

  1. Reads the EXACT required records from Resend  (needs a full-access Resend key)
  2. ADDS only those records to the Netlify DNS zone (needs a Netlify token).
     It NEVER edits or deletes any other record — it only creates the missing
     Resend records (idempotent: re-running skips ones already present).
  3. Triggers Resend verification and POLLS until the domain is "verified".
  4. Sends a REAL test email through Resend and polls its delivery status.

SAFETY
  • DRY-RUN by default. Nothing changes until you pass --apply.
  • Only creates the 3 Resend records; your apex A / www / everything else is
    left untouched (satisfies "do not change any other DNS records").
  • Secrets are read from env vars — do not hardcode them.

CREDENTIALS (create these, then export them)
  • NETLIFY_TOKEN      Netlify → User settings → Applications → Personal access tokens → New
  • RESEND_ADMIN_KEY   Resend → API Keys → Create (Full access). The send-only key
                       in the app CANNOT read domains or verify.
  • (optional) RESEND_SEND_KEY  a send key for step 4; defaults to RESEND_ADMIN_KEY.

USAGE
  export NETLIFY_TOKEN=nfp_xxx
  export RESEND_ADMIN_KEY=re_xxx
  # 1) DRY RUN — shows exactly what it would add, changes nothing:
  python3 fix_email_dns.py --domain caloriaclub.com --test-to you@example.com
  # 2) Do it for real:
  python3 fix_email_dns.py --domain caloriaclub.com --test-to you@example.com --apply
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RESEND = "https://api.resend.com"
NETLIFY = "https://api.netlify.com/api/v1"


def _call(method, url, token, *, json_body=None, form=None, accept_json=True):
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return json.loads(body) if (accept_json and body) else body
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[HTTP {e.code}] {method} {url}\n{e.read().decode()[:500]}") from e


# ----------------------------- Resend -------------------------------------- #
def resend_find_domain(key, domain):
    data = _call("GET", f"{RESEND}/domains", key)
    for d in (data.get("data") or data.get("domains") or []):
        if d.get("name") == domain:
            return d
    raise SystemExit(f"Domain {domain} not found in this Resend account.")


def resend_get(key, domain_id):
    return _call("GET", f"{RESEND}/domains/{domain_id}", key)


def resend_verify(key, domain_id):
    return _call("POST", f"{RESEND}/domains/{domain_id}/verify", key, json_body={})


# ----------------------------- Netlify ------------------------------------- #
def netlify_zone(token, domain):
    zones = _call("GET", f"{NETLIFY}/dns_zones", token)
    for z in zones:
        if z.get("name") == domain:
            return z
    raise SystemExit(f"No Netlify DNS zone named {domain}. Is DNS actually hosted in this Netlify account?")


def netlify_records(token, zone_id):
    return _call("GET", f"{NETLIFY}/dns_zones/{zone_id}/dns_records", token)


def netlify_create(token, zone_id, rec):
    return _call("POST", f"{NETLIFY}/dns_zones/{zone_id}/dns_records", token, json_body=rec)


# ------------------------------ helpers ------------------------------------ #
def fqdn(name, domain):
    name = (name or "").rstrip(".")
    if not name or name == "@":
        return domain
    return name if name.endswith(domain) else f"{name}.{domain}"


def required_records(resend_domain, domain):
    """Map Resend's records[] into normalized (type, hostname, value, priority)."""
    out = []
    for r in resend_domain.get("records", []):
        rtype = (r.get("type") or "").upper()
        if rtype not in ("MX", "TXT", "CNAME"):
            continue
        out.append({
            "type": rtype,
            "hostname": fqdn(r.get("name"), domain),
            "value": (r.get("value") or "").strip().strip('"'),
            "priority": int(r["priority"]) if r.get("priority") not in (None, "") else (10 if rtype == "MX" else None),
            "resend_status": r.get("status"),
            "resend_label": r.get("record"),
        })
    return out


def already_present(existing, req):
    for e in existing:
        if (e.get("type", "").upper() == req["type"]
                and (e.get("hostname") or "").rstrip(".") == req["hostname"].rstrip(".")
                and (e.get("value") or "").strip().strip('"') == req["value"]):
            return True
    return False


# ------------------------------- main -------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--test-to", help="Recipient for the real delivery test")
    ap.add_argument("--from", dest="mail_from", default="Paullina from Caloria <hello@caloriaclub.com>")
    ap.add_argument("--apply", action="store_true", help="Make changes (default: dry run)")
    ap.add_argument("--verify-timeout", type=int, default=900)
    args = ap.parse_args()

    netlify_token = os.environ.get("NETLIFY_TOKEN", "").strip()
    resend_admin = os.environ.get("RESEND_ADMIN_KEY", "").strip()
    resend_send = os.environ.get("RESEND_SEND_KEY", "").strip() or resend_admin
    if not resend_admin:
        raise SystemExit("Set RESEND_ADMIN_KEY (a FULL-ACCESS Resend key). The app's send-only key can't read/verify domains.")
    if not netlify_token:
        raise SystemExit("Set NETLIFY_TOKEN (Netlify personal access token).")

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== fix_email_dns  [{mode}]  domain={args.domain} ===\n")

    # 1) exact records from Resend
    dom = resend_find_domain(resend_admin, args.domain)
    dom = resend_get(resend_admin, dom["id"])
    print(f"Resend domain status: {dom.get('status')}  (id {dom['id']})")
    reqs = required_records(dom, args.domain)
    if not reqs:
        raise SystemExit("Resend returned no DNS records for this domain — check the domain in the dashboard.")
    print("Required records from Resend:")
    for r in reqs:
        pr = f" priority={r['priority']}" if r["priority"] is not None else ""
        print(f"  - {r['type']:5} {r['hostname']}{pr}  =  {r['value'][:60]}{'…' if len(r['value'])>60 else ''}")

    # 2) add the missing ones to Netlify (only-add, idempotent)
    zone = netlify_zone(netlify_token, args.domain)
    existing = netlify_records(netlify_token, zone["id"])
    print(f"\nNetlify zone {args.domain} (id {zone['id']}): {len(existing)} existing records — leaving all untouched.")
    created = 0
    for r in reqs:
        if already_present(existing, r):
            print(f"  = present   {r['type']} {r['hostname']}")
            continue
        payload = {"type": r["type"], "hostname": r["hostname"], "value": r["value"], "ttl": 3600}
        if r["priority"] is not None:
            payload["priority"] = r["priority"]
        if args.apply:
            netlify_create(netlify_token, zone["id"], payload)
            created += 1
            print(f"  + created   {r['type']} {r['hostname']}")
        else:
            print(f"  + WOULD ADD {r['type']} {r['hostname']}")
    if not args.apply:
        print("\nDRY RUN complete — re-run with --apply to add the records, verify, and test.")
        return
    print(f"\nCreated {created} record(s). Waiting for propagation, then verifying…")

    # 3) trigger verify + poll
    deadline = time.time() + args.verify_timeout
    status = dom.get("status")
    time.sleep(20)
    while time.time() < deadline:
        resend_verify(resend_admin, dom["id"])
        cur = resend_get(resend_admin, dom["id"])
        status = cur.get("status")
        print(f"  [{time.strftime('%H:%M:%S')}] Resend domain status: {status}")
        if status == "verified":
            break
        time.sleep(20)
    if status != "verified":
        raise SystemExit(f"Domain not verified within {args.verify_timeout}s (status={status}). "
                         "Records may still be propagating — re-run --apply later.")
    print("✅ Domain VERIFIED in Resend.")

    # 4) real delivery test
    if not args.test_to:
        print("No --test-to given; skipping the delivery test.")
        return
    print(f"\nSending a real test email to {args.test_to} …")
    sent = _call("POST", f"{RESEND}/emails", resend_send, json_body={
        "from": args.mail_from, "to": [args.test_to],
        "subject": "Caloria — email delivery test ✅",
        "html": "<p>This confirms Caloria can now send verification emails from "
                "<b>caloriaclub.com</b>. If you received this, registration email is fixed.</p>",
    })
    email_id = sent.get("id")
    print(f"  Resend accepted the email (id {email_id}). Polling delivery status…")
    for _ in range(24):  # ~2 min
        time.sleep(5)
        info = _call("GET", f"{RESEND}/emails/{email_id}", resend_admin)
        ev = info.get("last_event") or info.get("status")
        print(f"  delivery: {ev}")
        if ev in ("delivered", "bounced", "complained", "failed"):
            break
    print("\n=== DONE ===")
    print("If delivery shows 'delivered', registration verification emails are fixed.")


if __name__ == "__main__":
    main()
