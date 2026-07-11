#!/usr/bin/env python3
"""
Caloria Club — one-shot "Tomorrow" teaser to every waitlist member.

Runs on the Render shell of your PRODUCTION backend, where the database and
RESEND_API_KEY already live. It does NOT touch the website or the app.

USAGE (in the Render Shell):
    python send_tomorrow.py            # DRY RUN — shows the recipient count only
    python send_tomorrow.py test you@email.com   # send ONE test to yourself
    python send_tomorrow.py send       # actually send to everyone

Safe to re-run: every successful send is recorded, so a second run skips
anyone who already received it. Unsubscribed members are always skipped.
"""
import os, sys, json, time, sqlite3, urllib.request, urllib.error

DB       = os.environ.get("CALORIA_DB", "/data/caloria.db")
API_KEY  = os.environ.get("RESEND_API_KEY", "").strip()
FROM     = os.environ.get("EMAIL_FROM", "Caloria <onboarding@resend.dev>").strip()
REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "").strip()
BASE     = os.environ.get("APP_BASE_URL", "https://caloriaclub.com").rstrip("/")

CAMPAIGN  = "tomorrow_teaser"   # dedup key (kept in a tiny local table)
SPACING   = 0.6                 # seconds between sends (under Resend's rate limit)
SUBJECT   = "✨ Tomorrow."
PREHEADER = "Your Early Access begins tomorrow."

SERIF = "Georgia,'Times New Roman',serif"
SANS  = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


def _table_exists(c, name):
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _first_name(c, email):
    """First name from a matching account (waitlist rows have no name), else 'beautiful'."""
    local, _, dom = email.partition("@")
    local = local.split("+", 1)[0]
    canon = (local.replace(".", "") if dom in ("gmail.com", "googlemail.com") else local) + "@" + dom
    row = None
    if _table_exists(c, "users"):
        row = c.execute(
            "SELECT name FROM users WHERE lower(email)=? OR lower(email)=? ORDER BY id LIMIT 1",
            (email.lower(), canon.lower())).fetchone()
    if row and row[0] and row[0].strip():
        first = row[0].split()[0]
        if "@" not in first:
            return first[:40]
    return "beautiful"


def _render(first_name, email):
    url = BASE + "/"
    unlocks = "".join(
        f'<tr><td style="padding:5px 0;font-size:16px;line-height:1.5">✨&nbsp; {u}</td></tr>'
        for u in ["Early Access", "Founding Member pricing",
                  "Your exclusive Founding Member badge", "Full access before the public launch"])
    unsub = (f'<br><a href="{BASE}/api/club/unsubscribe?e={email}" '
             'style="color:#b7a6b0;text-decoration:underline">Unsubscribe</a>')
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Caloria</title></head>
<body style="margin:0;padding:0">
<div style="display:none;max-height:0;overflow:hidden">{PREHEADER}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>
<div style="margin:0;padding:34px 14px;background:linear-gradient(180deg,#fff6fb 0%,#fdeef6 55%,#eef9f8 100%)">
 <div style="max-width:500px;margin:0 auto;font-family:{SANS};color:#3a2937">
  <p style="text-align:center;margin:0 0 22px;font-family:{SERIF};font-size:25px;font-weight:700;letter-spacing:-.02em">◍ Caloria</p>
  <div style="background:#fff;border:1px solid #ffe6f1;border-radius:28px;padding:40px 32px;box-shadow:0 16px 44px rgba(242,77,140,.13)">
   <p style="text-align:center;margin:0 0 22px"><span style="display:inline-block;background:#fff5fa;color:#f24d8c;font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;padding:7px 16px;border-radius:100px;border:1px solid #ffd7e7">✦ Caloria Club</span></p>
   <h1 style="font-family:{SERIF};font-size:30px;font-weight:600;letter-spacing:-.015em;text-align:center;margin:0 0 24px;line-height:1.15">✨ Tomorrow.</h1>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">Hi {first_name},</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">Tomorrow is the day.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 18px">As one of our Founding Members, you'll receive your private invitation to <b>Caloria Club</b> before anyone else.</p>
   <p style="font-size:16px;line-height:1.6;margin:0 0 8px;font-weight:700">Tomorrow you'll unlock:</p>
   <table style="width:100%;border-collapse:collapse;margin:0 0 20px">{unlocks}</table>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">You're one day away.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 24px">See you tomorrow.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 26px">— Caloria Club</p>
   <p style="text-align:center;margin:0"><a href="{url}" style="display:inline-block;background:linear-gradient(135deg,#ff9ec4,#f24d8c);color:#fff;text-decoration:none;padding:15px 34px;border-radius:100px;font-weight:700;font-size:16px">See You Tomorrow</a></p>
  </div>
  <p style="text-align:center;color:#b7a6b0;font-size:12px;margin:22px 0 0;line-height:1.6">Caloria Club · made with \U0001f90d for the first women inside.{unsub}</p>
 </div>
</div></body></html>"""
    text = (f"Hi {first_name},\n\nTomorrow is the day.\n\n"
            "As one of our Founding Members, you'll receive your private invitation to "
            "Caloria Club before anyone else.\n\nTomorrow you'll unlock:\n"
            "  ✨ Early Access\n  ✨ Founding Member pricing\n"
            "  ✨ Your exclusive Founding Member badge\n  ✨ Full access before the public launch\n\n"
            f"You're one day away.\n\nSee you tomorrow.\n\n— Caloria Club\n\nSee you tomorrow: {url}")
    return html, text


def _send_one(to, html, text):
    payload = {"from": FROM, "to": [to], "subject": SUBJECT, "html": html, "text": text,
               "headers": {"List-Unsubscribe": f"<{BASE}/api/club/unsubscribe?e={to}>",
                           "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}}
    if REPLY_TO:
        payload["reply_to"] = [REPLY_TO]
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json",
                 "User-Agent": "Caloria/1.0 (+https://caloriaclub.com)"}, method="POST")
    resp = urllib.request.urlopen(req, timeout=20)
    return json.loads(resp.read()).get("id")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "dry"
    if not API_KEY:
        print("ERROR: RESEND_API_KEY is not set in this environment."); return
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS campaign_sends "
              "(email TEXT, campaign TEXT, sent_at TEXT DEFAULT CURRENT_TIMESTAMP, "
              "PRIMARY KEY(email, campaign))")
    c.commit()

    if mode == "test":
        to = sys.argv[2] if len(sys.argv) > 2 else ""
        if not to:
            print("Usage: python send_tomorrow.py test you@email.com"); return
        html, text = _render(_first_name(c, to), to)
        print("Sending ONE test to", to, "...")
        print("  id:", _send_one(to, html, text))
        return

    rows = c.execute(
        "SELECT email FROM club_members WHERE unsubscribed = 0 "
        "AND email NOT IN (SELECT email FROM campaign_sends WHERE campaign = ?) ORDER BY id",
        (CAMPAIGN,)).fetchall()
    already = c.execute("SELECT COUNT(*) FROM campaign_sends WHERE campaign = ?", (CAMPAIGN,)).fetchone()[0]
    unsub = c.execute("SELECT COUNT(*) FROM club_members WHERE unsubscribed = 1").fetchone()[0]
    total = c.execute("SELECT COUNT(*) FROM club_members").fetchone()[0]
    emails = [r[0] for r in rows]

    print(f"Waitlist total: {total}   |   will send: {len(emails)}   |   "
          f"skip (unsubscribed): {unsub}   |   skip (already sent): {already}")
    print(f"From: {FROM}")
    if mode != "send":
        print("\nDRY RUN — no emails sent. To send for real, run:  python send_tomorrow.py send")
        return

    sent = failed = 0
    for i, to in enumerate(emails, 1):
        html, text = _render(_first_name(c, to), to)
        try:
            _send_one(to, html, text)
            c.execute("INSERT OR IGNORE INTO campaign_sends (email, campaign) VALUES (?, ?)", (to, CAMPAIGN))
            c.commit()
            sent += 1
        except urllib.error.HTTPError as e:
            failed += 1
            print(f"  FAILED {to}: HTTP {e.code} {e.read().decode('utf-8','ignore')[:120]}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAILED {to}: {e}")
        if i % 25 == 0:
            print(f"  ...{i}/{len(emails)}  (sent {sent}, failed {failed})")
        time.sleep(SPACING)

    print("\n===== DELIVERY REPORT =====")
    print(f"  Sent:    {sent}")
    print(f"  Failed:  {failed}")
    print(f"  Skipped: {unsub + already}  ({unsub} unsubscribed, {already} already sent)")
    print(f"  Total waitlist: {total}")


if __name__ == "__main__":
    main()
