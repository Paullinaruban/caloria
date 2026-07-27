#!/usr/bin/env python3
"""
send_relaunch_email.py — one-time "we're back + $15 relaunch" email to EVERYONE
who ever attempted to register / was issued a verification code.

Recipients = the `users` table:  SELECT email FROM users
This is the exact population behind the admin "Total Members" number
(SELECT COUNT(*) FROM users). auth.signup() inserts a users row BEFORE sending
the verification email, so failed-verification accounts (email_verified = 0) are
included. No usage/active/paid filter. Then deduped; admin + obvious test/dev
addresses removed (test filter is conservative and reviewable; --no-test-filter
keeps everyone).

Reuses the app's existing Resend integration (email_send._send), so it sends
from the verified caloriaclub.com domain with the same key the app uses.

SAFETY / CORRECTNESS
  • Sends nothing until you pass --send (and confirm); --test emails only you.
  • Recipient list is byte-for-byte the admin "All members" page (usage.dashboard).
  • De-duplicates addresses case-insensitively; excludes test/dev + admin accounts.
  • Idempotent + resumable: every send is recorded in a `relaunch_sends` table,
    so re-running never emails the same person twice (safe after an interruption
    or rate-limit).
  • Throttled (--throttle) to protect domain reputation right after the DNS fix.
  • Includes an unsubscribe line + List-Unsubscribe header (bulk-mail hygiene).
  • Prints a summary: found, skipped (by reason), sent OK, failed.

MUST RUN WHERE THE PRODUCTION DB + VERIFIED DOMAIN LIVE (i.e. on Render), so it
reads the real user list and sends from the verified domain.

USAGE
  # 1) DRY RUN — shows the recipient count (matches the admin page), sends nothing:
  python3 send_relaunch_email.py
  # 2) Send ONE test copy to yourself and eyeball it:
  python3 send_relaunch_email.py --test you@youremail.com
  # 3) Warm-up batch (recommended): asks you to type SEND, then emails 25:
  python3 send_relaunch_email.py --send --limit 25 --throttle 0.7
  # 4) Full campaign (re-run — already-sent addresses are skipped automatically):
  python3 send_relaunch_email.py --send --throttle 0.5
"""
import argparse
import os
import re
import sys
import time

# Work whether this file sits next to the backend modules (flat layout) or inside
# a scripts/ subfolder — find the directory that contains config.py.
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here if os.path.exists(os.path.join(_here, "config.py")) else os.path.dirname(_here))
import config
import db
import email_send

SUBJECT = "We fixed it ❤️ Your Caloria account is ready"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Obvious test / developer / placeholder addresses to EXCLUDE. Deliberately
# CONSERVATIVE (anchored to throwaway domains + synthetic prefixes/ids) so it
# can't accidentally drop a real user like "qadir@gmail.com". The dry run prints
# everything it excludes so you can review; use --no-test-filter to keep all.
_TEST_RE = re.compile(
    r"@(?:test|example|invalid|localhost|mailinator)\.[a-z.]+$"          # throwaway/test domains
    r"|@t\.com$"                                                          # @t.com (QA harness)
    r"|^(?:audit|browserqa|browserscan|codeflow|dashseed|smoke|e2e|fixture|seed)[-_0-9a-z]*@"  # QA prefixes
    r"|(?:^|[._-])test\d*(?:[._-]|@)"                                     # 'test' as a token (not mid-word)
    r"|_\d{8,}@|\d{12,}@",                                                # long synthetic numeric ids
    re.I)

BODY_TEXT = """Hi,

A few days ago we had a technical problem with our email verification system.
Because of it, many people who signed up never received their login code — so you
may have tried to join Caloria and couldn't get in. I'm so sorry about that.

It's now completely fixed. Your account is ready and you can log in normally.

👉 Try again here: https://caloriaclub.com

As an apology for the trouble, your membership is just $15/month until August 5.
After August 5 it automatically returns to the regular $19.99/month — so this is
the best time to start.

Inside Caloria you'll get:
• AI Food Scanner
• Personalized Meal Plans
• Workout Plans
• AI Coach
• Wellness Community
• And much more

Thank you so much for giving Caloria a try.
Love,
Paulina ❤️

---
You received this because you signed up at caloriaclub.com.
To unsubscribe, reply to this email with "unsubscribe".
"""

BODY_HTML = """<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;color:#2a1a24;line-height:1.6">
  <p>Hi,</p>
  <p>A few days ago we had a technical problem with our email verification system.
  Because of it, many people who signed up never received their login code — so you may
  have tried to join Caloria and couldn't get in. <b>I'm so sorry about that.</b></p>
  <p>It's now <b>completely fixed.</b> Your account is ready and you can log in normally.</p>
  <p style="text-align:center;margin:26px 0">
    <a href="https://caloriaclub.com" style="display:inline-block;background:#e85a9b;color:#fff;
    text-decoration:none;padding:14px 30px;border-radius:100px;font-weight:700;font-size:16px">
    Log in to Caloria →</a>
  </p>
  <p>As an apology for the trouble, your membership is just <b>$15/month until August 5.</b>
  After August 5 it automatically returns to the regular $19.99/month — so this is the
  best time to start.</p>
  <p>Inside Caloria you'll get:</p>
  <ul style="padding-left:1.1em">
    <li>AI Food Scanner</li>
    <li>Personalized Meal Plans</li>
    <li>Workout Plans</li>
    <li>AI Coach</li>
    <li>Wellness Community</li>
    <li>And much more</li>
  </ul>
  <p>Thank you so much for giving Caloria a try.<br>Love,<br><b>Paulina ❤️</b></p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
  <p style="font-size:12px;color:#999">You received this because you signed up at caloriaclub.com.
  To unsubscribe, reply with "unsubscribe".</p>
</div>"""

UNSUB_HEADERS = {"List-Unsubscribe": "<mailto:hello@caloriaclub.com?subject=unsubscribe>"}


def ensure_log_table():
    with db.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS relaunch_sends (
            email TEXT PRIMARY KEY, status TEXT, message_id TEXT, error TEXT, sent_at TEXT
        )""")


def collect_emails(apply_test_filter=True):
    """Recipients = EVERY email in the production `users` table — the exact source
    of the admin 'Total Members' count (SELECT COUNT(*) FROM users).

    auth.signup() INSERTs a users row the moment someone submits the signup form,
    BEFORE the verification email, so this table already contains everyone who
    attempted to register / was issued a verification code, verified or not
    (email_verified 0 or 1). No usage/active/paid filter.

    Effective recipient query:
        SELECT DISTINCT email FROM users WHERE email IS NOT NULL AND email <> '';
    (We SELECT all rows — not DISTINCT — only so we can REPORT the duplicate count;
    de-duplication here is case-insensitive, i.e. stronger than SQL DISTINCT.)

    Returns (recipients, stats, excluded_test). Then: dedupe + exclude admin +
    (optionally) exclude obvious test/dev addresses."""
    admins = {str(e).strip().lower() for e in (config.ADMIN_EMAILS or [])}
    with db.cursor() as c:
        total_users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        rows = c.execute(
            "SELECT email FROM users WHERE email IS NOT NULL AND TRIM(email) <> '' ORDER BY id"
        ).fetchall()

    empty_or_null = total_users - len(rows)     # NULL/'' emails filtered by SQL
    regex_invalid = 0
    valid = []
    for r in rows:
        e = (r["email"] or "").strip().lower()
        if not _EMAIL_RE.match(e):
            regex_invalid += 1; continue
        valid.append(e)

    unique = list(dict.fromkeys(valid))          # case-insensitive dedupe, order preserved
    duplicates = len(valid) - len(unique)

    recipients, excluded_test, admin_excluded, test_excluded = [], [], 0, 0
    for e in unique:
        if apply_test_filter and _TEST_RE.search(e):
            test_excluded += 1; excluded_test.append(e); continue
        if e in admins:
            admin_excluded += 1; continue
        recipients.append(e)

    stats = {
        "total_users": total_users,              # = Admin → Total Members
        "unique_emails": len(unique),
        "duplicates": duplicates,
        "invalid": empty_or_null + regex_invalid,
        "admin": admin_excluded,
        "test": test_excluded,
        "final": len(recipients),
    }
    return recipients, stats, excluded_test


def already_sent():
    with db.cursor() as c:
        return {r["email"] for r in c.execute("SELECT email FROM relaunch_sends WHERE status='ok'")}


def record(email, status, message_id=None, error=None):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db.cursor() as c:
        c.execute("INSERT OR REPLACE INTO relaunch_sends (email,status,message_id,error,sent_at) VALUES (?,?,?,?,?)",
                  (email, status, message_id, (error or "")[:300], now))


def main():
    ap = argparse.ArgumentParser(description="Caloria relaunch email campaign (production-safe).")
    ap.add_argument("--test", metavar="EMAIL", help="Send ONE test copy to this address, then exit (not logged as a campaign send)")
    ap.add_argument("--send", action="store_true", help="Send the real campaign to all recipients (asks for confirmation)")
    ap.add_argument("--yes", action="store_true", help="Skip the interactive confirmation (for non-interactive runs)")
    ap.add_argument("--limit", type=int, default=0, help="Send to at most N new addresses (warm-up batch)")
    ap.add_argument("--throttle", type=float, default=0.5, help="Seconds to sleep between sends")
    ap.add_argument("--no-test-filter", action="store_true", help="Do NOT exclude test/dev addresses (email literally every users row)")
    args = ap.parse_args()

    ensure_log_table()

    # --- one test email to yourself (no campaign, not recorded) ---
    if args.test:
        print(f"Sending ONE test email to {args.test} …")
        try:
            mid = email_send._send(args.test, SUBJECT, BODY_HTML, BODY_TEXT, headers=UNSUB_HEADERS)
            print(f"  OK — sent (message id: {mid}). Check that inbox, then run --send.")
        except Exception as ex:  # noqa: BLE001
            print(f"  FAILED: {ex}")
        return

    all_emails, stats, excluded_test = collect_emails(apply_test_filter=not args.no_test_filter)
    done = already_sent()
    pending = [e for e in all_emails if e not in done]
    if args.limit:
        pending = pending[:args.limit]

    print("=" * 64)
    print(f"  RELAUNCH EMAIL  [{'SEND' if args.send else 'DRY RUN'}]  —  source: production users table")
    print("  " + "-" * 60)
    print(f"  Total users (= Admin Total Members) : {stats['total_users']}")
    print(f"  Total unique emails                 : {stats['unique_emails']}")
    print(f"  Duplicate emails                    : {stats['duplicates']}")
    print(f"  Invalid / empty emails              : {stats['invalid']}")
    print(f"  Admin emails excluded               : {stats['admin']}")
    print(f"  Test/dev emails excluded            : {stats['test']}"
          + ("" if not args.no_test_filter else "   (filter disabled)"))
    print(f"  FINAL recipient count               : {stats['final']}")
    print("  " + "-" * 60)
    print(f"  already sent (will skip)            : {len(done)}")
    print(f"  to send THIS run                    : {len(pending)}"
          + (f"  (capped at --limit {args.limit})" if args.limit else ""))
    print(f"  email domain ready                  : {config.email_ready()}  from={config.EMAIL_FROM}")
    print("=" * 64)

    if not args.send:
        if excluded_test:
            print(f"\n  Excluded as test/dev ({len(excluded_test)}) — REVIEW that none are real users:")
            for e in excluded_test:
                print(f"    - {e}")
        print(f"\n  Recipients ({len(pending)}):")
        for e in pending[:20]:
            print(f"    -> {e}")
        if len(pending) > 20:
            print(f"    … and {len(pending) - 20} more")
        print("\nDRY RUN — nothing sent. Next: `--test you@youremail.com`, then `--send`.")
        return

    if not pending:
        print("Nothing to send — everyone has already received it.")
        return

    # confirmation gate before the real campaign
    if not args.yes:
        ans = input(f"\nType SEND to email {len(pending)} recipients now: ").strip()
        if ans != "SEND":
            print("Aborted — no emails sent.")
            return

    sent = failed = 0
    for i, e in enumerate(pending, 1):
        try:
            mid = email_send._send(e, SUBJECT, BODY_HTML, BODY_TEXT, headers=UNSUB_HEADERS)
            record(e, "ok", message_id=mid)
            sent += 1
            print(f"  [{i}/{len(pending)}] OK    {e}  ({mid})")
        except Exception as ex:  # noqa: BLE001 — one bad address must not stop the run
            record(e, "failed", error=str(ex))
            failed += 1
            print(f"  [{i}/{len(pending)}] FAIL  {e}  ({ex})")
        time.sleep(args.throttle)

    print("-" * 64)
    print(f"  sent OK : {sent}")
    print(f"  failed  : {failed}  (see relaunch_sends table for reasons)")
    print(f"  remaining unsent addresses: {len([e for e in all_emails if e not in already_sent()])}")
    print("=" * 64)


if __name__ == "__main__":
    main()
