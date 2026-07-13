"""Caloria Club — the Founding Members experience.

Not a waitlist: an exclusive founding-members club. Visitors join with just an
email, receive a unique referral code, and move up the founders list by inviting
friends. Powers the landing-page Club section, the /club.html success page, the
welcome email, and the admin Club dashboard (search / leaderboard / CSV export /
founder updates via Resend).

Tables (created in db.init_db):
  club_members — email, referral code, referral count, join date
  club_updates — founder-update broadcast log (subject, progress, status)
"""
from __future__ import annotations

import re
import secrets
import threading
import time

import config
import db
import email_send

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Referral codes avoid look-alike characters (0/O, 1/I/L) — they get read
# aloud and retyped from Instagram stories.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8
# Resend's default rate limit is 2 req/s — space broadcast sends safely under it.
_BROADCAST_SPACING = 0.6


class ClubError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


def _canonical_email(email: str) -> str:
    """Collapse alias tricks so one inbox equals one membership: strip +tags
    everywhere (delivery-equivalent on all major providers) and remove dots for
    Gmail, where they're ignored. The ORIGINAL address is kept for delivery."""
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return f"{local}@{domain}"


def init() -> None:
    """Startup pass (after db.init_db): backfill canonical addresses for members
    created before the column existed. Idempotent."""
    with db.cursor() as c:
        rows = c.execute(
            "SELECT id, email FROM club_members WHERE canonical IS NULL OR canonical = ''"
        ).fetchall()
        for r in rows:
            c.execute("UPDATE club_members SET canonical = ? WHERE id = ?",
                      (_canonical_email(r["email"]), r["id"]))
    if rows:
        print(f"[caloria][club] backfilled canonical email for {len(rows)} member(s)")


# ---------- joining ----------
def _new_code(c) -> str:
    while True:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
        if not c.execute("SELECT 1 FROM club_members WHERE referral_code = ?", (code,)).fetchone():
            return code


def join(email: str, ref: str = "") -> dict:
    """Add a founding member. Idempotent: an existing member gets her current
    status back (with already=True) instead of an error — 'you're already in'
    is part of the experience, not a failure."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise ClubError("Please enter a valid email address.")
    ref = (ref or "").strip().upper()[:_CODE_LEN]

    canonical = _canonical_email(email)
    with db.cursor() as c:
        # Dedup on the canonical form — girl@x, girl+vip@x and g.irl@gmail are
        # all one membership (blocks referral farming via alias signups).
        existing = c.execute(
            "SELECT * FROM club_members WHERE canonical = ? OR email = ? "
            "ORDER BY id LIMIT 1", (canonical, email),
        ).fetchone()
        if existing:
            out = _status(c, existing)
            out["already"] = True
            return out
        referrer = None
        if ref:
            referrer = c.execute(
                "SELECT id, email, referral_code FROM club_members WHERE referral_code = ?",
                (ref,),
            ).fetchone()
        code = _new_code(c)
        c.execute(
            "INSERT INTO club_members (email, canonical, referral_code, referred_by) "
            "VALUES (?,?,?,?)",
            (email, canonical, code, referrer["referral_code"] if referrer else None),
        )
        member_id = c.lastrowid
        if referrer and referrer["email"] != email:
            c.execute(
                "UPDATE club_members SET referral_count = referral_count + 1 WHERE id = ?",
                (referrer["id"],),
            )
        row = c.execute("SELECT * FROM club_members WHERE id = ?", (member_id,)).fetchone()
        out = _status(c, row)
        out["already"] = False

    # Welcome email must never block or fail the signup.
    threading.Thread(target=_send_welcome, args=(member_id, email, out), daemon=True).start()
    return out


def _status(c, row) -> dict:
    """Member status payload: position is computed live from the ranking
    (referrals first, then seniority), so inviting friends moves you up."""
    position = c.execute(
        "SELECT COUNT(*) + 1 AS pos FROM club_members "
        "WHERE referral_count > ? OR (referral_count = ? AND id < ?)",
        (row["referral_count"], row["referral_count"], row["id"]),
    ).fetchone()["pos"]
    total = c.execute("SELECT COUNT(*) AS n FROM club_members").fetchone()["n"]
    return {
        "position": position,
        "total": total,
        "referral_code": row["referral_code"],
        "referral_count": row["referral_count"],
        "referral_link": referral_link(row["referral_code"]),
        "joined_at": row["created_at"],
    }


def referral_link(code: str) -> str:
    return f"{config.APP_BASE_URL.rstrip('/')}/?ref={code}#club"


def _success_link(code: str) -> str:
    return f"{config.APP_BASE_URL.rstrip('/')}/club.html?m={code}"


def status_by_code(code: str) -> dict | None:
    code = (code or "").strip().upper()[:_CODE_LEN]
    if not code:
        return None
    with db.cursor() as c:
        row = c.execute("SELECT * FROM club_members WHERE referral_code = ?", (code,)).fetchone()
        return _status(c, row) if row else None


def stats() -> dict:
    """Public social proof for the landing section (count only — no emails)."""
    with db.cursor() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM club_members").fetchone()["n"]
    return {"total": total}


# ---------- onboarding answers ----------
# The three founding questions asked right after the email step. Saved one tap
# at a time (fire-and-forget from the client) so even a girl who closes the tab
# mid-flow leaves usable insight behind.
_ANSWER_FIELDS = ("goal", "struggle", "excited")


def save_answers(code: str, data: dict) -> dict:
    code = (code or "").strip().upper()[:_CODE_LEN]
    updates = {}
    for field in _ANSWER_FIELDS:
        v = data.get(field)
        if isinstance(v, str) and v.strip():
            updates[field] = v.strip()[:200]
    if not code or not updates:
        raise ClubError("Nothing to save.")
    with db.cursor() as c:
        row = c.execute("SELECT id FROM club_members WHERE referral_code = ?", (code,)).fetchone()
        if not row:
            raise ClubError("We couldn't find that membership.", 404)
        sets = ", ".join(f"{f} = ?" for f in updates)          # keys are whitelisted above
        c.execute(f"UPDATE club_members SET {sets} WHERE id = ?",
                  (*updates.values(), row["id"]))
    return {"ok": True, "saved": sorted(updates)}


# ---------- welcome email (the founder letter) ----------
def _send_welcome(member_id: int, email: str, status: dict) -> None:
    try:
        # Subject keeps her exact words; the emoji moved to the end — leading
        # emojis are a known negative with Microsoft/Yahoo spam filters.
        html = _welcome_html(status, email)
        text = _welcome_text(status)
        email_send._send(email, "You’re officially a Founding Member 🤍", html, text,
                         headers=_unsub_headers(email))
        _bump_email_counter()
        with db.cursor() as c:
            c.execute("UPDATE club_members SET welcome_sent = 1 WHERE id = ?", (member_id,))
    except Exception as e:  # noqa: BLE001 — email problems must not break joining
        print(f"[caloria][club] welcome email failed for {email}: {e}")


# The email mirrors the site's design system: cream/blush gradients, serif
# display headings, rounded cards, rose-gradient buttons. Inline styles only
# (email clients strip <style> blocks); Georgia stands in for Fraunces.
_SERIF = "Georgia,'Times New Roman',serif"
_SANS = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


def _email_doc(inner: str, preheader: str) -> str:
    """Wrap template content in a complete, well-formed HTML document —
    structural completeness scores better with spam filters than bare
    fragments — plus a hidden preheader (the preview line shown next to the
    subject in the inbox; strong open-rate → sender-reputation signal)."""
    pre = (preheader or "").strip()[:110]
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Caloria</title></head><body style="margin:0;padding:0">'
        f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all">'
        f'{pre}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>'
        f'{inner}</body></html>'
    )


def _welcome_html(s: dict, recipient_email: str = "") -> str:
    """Paullina's founder letter — intimate, personal, minimal chrome. The only
    product touch is a quiet P.S. with her referral link and status page."""
    link = s["referral_link"]
    my_page = _success_link(s["referral_code"])
    unsub = (
        f' · <a href="{_unsubscribe_link(recipient_email)}" '
        'style="color:#8a7886;text-decoration:underline">Unsubscribe</a>'
        if recipient_email else ""
    )
    letter = [
        "Hi beautiful🩵",
        "I honestly can’t believe you’re here.",
        "A year ago, Caloria only existed as random notes inside my Notes app.",
        "Today, you’re one of the very first people helping me bring it to life.",
        "Thank you.",
        "Seriously.",
        "This isn’t just another waitlist.",
        "You’re officially one of Caloria’s <b>Founding Members</b>.",
        "When Caloria launches, you’ll receive <b>exclusive early access</b> before everyone else.",
        "Thank you for believing in this before anyone else did.",
    ]
    body = "".join(
        f'<p style="font-size:16px;line-height:1.75;margin:0 0 18px">{p}</p>' for p in letter
    )
    return _email_doc(f"""\
<div style="margin:0;padding:32px 12px;background:linear-gradient(180deg,#fff6fb 0%,#fdeef6 60%,#eef9f8 100%)">
 <div style="max-width:520px;margin:0 auto;font-family:{_SANS};color:#3a2937">

  <p style="text-align:center;margin:0 0 18px;font-family:{_SERIF};font-size:24px;font-weight:700;letter-spacing:-.02em;color:#3a2937">◍ Caloria</p>

  <div style="background:rgba(255,255,255,.94);border:1px solid #ffe6f1;border-radius:30px;padding:42px 34px;box-shadow:0 16px 44px rgba(242,77,140,.14)">
   <p style="text-align:center;margin:0 0 26px"><span style="display:inline-block;background:#fff5fa;color:#f24d8c;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:7px 16px;border-radius:100px;border:1px solid #ffd7e7">✦ Founding Member №{s['position']}</span></p>

   {body}

   <p style="font-size:16px;line-height:1.75;margin:6px 0 0">Love,</p>
   <p style="font-family:{_SERIF};font-style:italic;font-size:22px;margin:4px 0 0">Paullina Ruban🥹</p>

   <div style="border-top:1px solid #ffe6f1;margin:30px 0 0;padding:22px 0 0">
    <p style="color:#8a7886;font-size:13.5px;line-height:1.65;margin:0 0 10px"><b style="color:#f24d8c">P.S.</b> If you have a girlfriend who'd love to be part of this too, this little link is yours — every friend who joins moves you higher up the founders list:</p>
    <p style="margin:0 0 14px;background:#fff5fa;border:1px dashed #ffd7e7;border-radius:14px;padding:11px;font-size:12.5px;word-break:break-all;text-align:center"><a href="{link}" style="color:#f24d8c;text-decoration:none;font-weight:600">{link}</a></p>
    <p style="margin:0;text-align:center"><a href="{my_page}" style="display:inline-block;background:linear-gradient(135deg,#ff9ec4,#f24d8c);color:#ffffff;text-decoration:none;padding:12px 26px;border-radius:100px;font-weight:700;font-size:14px">See my founder status</a></p>
   </div>
  </div>

  <p style="text-align:center;color:#8a7886;font-size:12px;margin:22px 0 0;line-height:1.6">You're receiving this because you became a Caloria Club Founding Member.<br>◍ Caloria — helping girls build healthy habits without restriction.{unsub}</p>
 </div>
</div>""", f"Founding Member №{s['position']} — your personal letter from Paullina is inside.")


def _welcome_text(s: dict) -> str:
    return (
        "Hi beautiful 🩵\n\n"
        "I honestly can’t believe you’re here.\n\n"
        "A year ago, Caloria only existed as random notes inside my Notes app.\n\n"
        "Today, you’re one of the very first people helping me bring it to life.\n\n"
        "Thank you.\n\nSeriously.\n\n"
        "This isn’t just another waitlist.\n\n"
        "You’re officially one of Caloria’s Founding Members.\n\n"
        "When Caloria launches, you’ll receive exclusive early access before everyone else.\n\n"
        "Thank you for believing in this before anyone else did.\n\n"
        "Love,\nPaullina Ruban 🥹\n\n"
        "P.S. If you have a girlfriend who'd love to be part of this too, this link is "
        f"yours — every friend who joins moves you up the founders list:\n{s['referral_link']}\n\n"
        f"See your founder status any time:\n{_success_link(s['referral_code'])}"
    )


# ---------- unsubscribe (one-click, HMAC-signed — no login needed) ----------
def unsubscribe_token(email: str) -> str:
    import hashlib
    import hmac as _hmac
    return _hmac.new(config.APP_SECRET.encode(), ("club-unsub:" + email).encode(),
                     hashlib.sha256).hexdigest()[:32]


def _unsubscribe_link(email: str) -> str:
    from urllib.parse import quote
    return (f"{config.APP_BASE_URL.rstrip('/')}/api/club/unsubscribe"
            f"?e={quote(email)}&t={unsubscribe_token(email)}")


def _bump_email_counter() -> None:
    """Count every real club email sent today (UTC) — the dashboard shows it
    against Resend's free-plan daily limit. Best-effort; never blocks a send."""
    import datetime
    key = "club_emails:" + datetime.datetime.utcnow().date().isoformat()
    try:
        with db.cursor() as c:
            c.execute("INSERT INTO kv (k, v) VALUES (?, '1') "
                      "ON CONFLICT(k) DO UPDATE SET v = CAST(v AS INTEGER) + 1", (key,))
    except Exception as e:  # noqa: BLE001
        print(f"[caloria][club] email counter failed: {e}")


def emails_sent_today() -> int:
    import datetime
    key = "club_emails:" + datetime.datetime.utcnow().date().isoformat()
    try:
        return int(db.kv_get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _unsub_headers(email: str) -> dict:
    """RFC 8058 one-click unsubscribe headers — Gmail/Yahoo bulk-sender
    requirements; mail clients surface their own Unsubscribe button."""
    return {
        "List-Unsubscribe": f"<{_unsubscribe_link(email)}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def unsubscribe(email: str, token: str) -> bool:
    """Mark a member unsubscribed if the signed token matches. Idempotent."""
    import hmac as _hmac
    email = (email or "").strip().lower()
    if not email or not _hmac.compare_digest(token or "", unsubscribe_token(email)):
        return False
    with db.cursor() as c:
        c.execute("UPDATE club_members SET unsubscribed = 1 WHERE email = ?", (email,))
        return True


UNSUBSCRIBE_PAGE = f"""\
<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Caloria Club</title></head>
<body style="margin:0;padding:60px 16px;background:linear-gradient(180deg,#fff6fb,#fdeef6);font-family:{_SANS};color:#3a2937;text-align:center">
<div style="max-width:440px;margin:0 auto;background:rgba(255,255,255,.94);border:1px solid #ffe6f1;border-radius:30px;padding:44px 30px;box-shadow:0 16px 44px rgba(242,77,140,.14)">
<p style="font-family:{_SERIF};font-size:22px;font-weight:700;margin:0 0 14px">◍ Caloria</p>
<h1 style="font-family:{_SERIF};font-size:24px;font-weight:600;margin:0 0 10px">%HEADING%</h1>
<p style="color:#8a7886;font-size:15px;line-height:1.65;margin:0">%BODY%</p></div></body></html>"""


# ---------- personalization ----------
# Merge tags usable in founder-update subjects & messages.
def _member_vars(c, row) -> dict:
    return {
        "position": str(_status(c, row)["position"]),
        "referral_count": str(row["referral_count"]),
        "referral_link": referral_link(row["referral_code"]),
        "referral_code": row["referral_code"],
        "email": row["email"],
    }


_SAMPLE_VARS = {
    "position": "1", "referral_count": "3",
    "referral_link": referral_link("EXAMPLE1"), "referral_code": "EXAMPLE1",
    "email": "member@example.com",
}


def _personalize(text: str, tags: dict) -> str:
    for k, v in tags.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ---------- audiences (broadcast segments) ----------
# label + SQL predicate. Unsubscribed members are ALWAYS excluded.
_AUDIENCES = {
    "all":        ("All members", "unsubscribed = 0"),
    "referrers":  ("Top referrers (1+ referrals)", "unsubscribed = 0 AND referral_count > 0"),
    "first_100":  ("First 100 members", "unsubscribed = 0 AND id IN (SELECT id FROM club_members ORDER BY id LIMIT 100)"),
    "first_1000": ("First 1,000 members", "unsubscribed = 0 AND id IN (SELECT id FROM club_members ORDER BY id LIMIT 1000)"),
    "invited":    ("Invited members (joined via referral)", "unsubscribed = 0 AND referred_by IS NOT NULL"),
}


def audience_count(audience: str) -> dict:
    label, where = _AUDIENCES.get(audience or "all", _AUDIENCES["all"])
    with db.cursor() as c:
        n = c.execute(f"SELECT COUNT(*) AS n FROM club_members WHERE {where}").fetchone()["n"]
    return {"audience": audience, "label": label, "count": n}


# ---------- founder updates (admin broadcast via Resend) ----------
def _update_html(subject: str, message: str, recipient_email: str = "") -> str:
    paragraphs = "".join(
        f'<p style="font-size:15px;line-height:1.7;margin:0 0 16px">{p}</p>'
        for p in (message or "").strip().split("\n\n") if p.strip()
    ) or '<p style="font-size:15px;line-height:1.7;margin:0 0 16px"></p>'
    paragraphs = paragraphs.replace("\n", "<br>")
    unsub = (
        f'<br><a href="{_unsubscribe_link(recipient_email)}" '
        'style="color:#8a7886;text-decoration:underline">Unsubscribe</a>'
        if recipient_email else ""
    )
    return _email_doc(f"""\
<div style="margin:0;padding:32px 12px;background:linear-gradient(180deg,#fff6fb 0%,#fdeef6 60%,#eef9f8 100%)">
 <div style="max-width:520px;margin:0 auto;font-family:{_SANS};color:#3a2937">
  <p style="text-align:center;margin:0 0 18px;font-family:{_SERIF};font-size:24px;font-weight:700;letter-spacing:-.02em">◍ Caloria</p>
  <div style="background:rgba(255,255,255,.92);border:1px solid #ffe6f1;border-radius:30px;padding:38px 32px;box-shadow:0 16px 44px rgba(242,77,140,.14)">
   <p style="text-align:center;margin:0 0 14px"><span style="display:inline-block;background:#fff5fa;color:#f24d8c;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:7px 16px;border-radius:100px;border:1px solid #ffd7e7">✦ Founder Update</span></p>
   <h1 style="font-family:{_SERIF};font-size:26px;font-weight:600;letter-spacing:-.015em;text-align:center;margin:0 0 22px;line-height:1.2">{subject}</h1>
   {paragraphs}
   <p style="font-size:15px;line-height:1.7;margin:16px 0 0">With love,<br><span style="font-family:{_SERIF};font-style:italic;font-size:17px">Paullina Ruban</span><br><span style="color:#8a7886;font-size:13px">Founder, Caloria</span></p>
  </div>
  <p style="text-align:center;color:#8a7886;font-size:12px;margin:22px 0 0;line-height:1.6">Sent exclusively to Caloria Club Founding Members — you hear everything first. 🤍{unsub}</p>
 </div>
</div>""", (message or "").strip().split("\n")[0][:110])


def _update_text(subject: str, message: str, recipient_email: str = "") -> str:
    text = f"{subject}\n\n{message}\n\nWith love,\nPaullina Ruban\nFounder, Caloria"
    if recipient_email:
        text += f"\n\nUnsubscribe: {_unsubscribe_link(recipient_email)}"
    return text


def _validate_update(subject: str, message: str) -> tuple:
    subject = (subject or "").strip()[:200]
    message = (message or "").strip()[:20000]
    if not subject or not message:
        raise ClubError("A subject and a message are both required.")
    return subject, message


def preview_update(subject: str, message: str) -> dict:
    """Rendered preview of a founder update, with merge tags filled from sample
    data — exactly what a member will receive (minus her real numbers)."""
    subject, message = _validate_update(subject, message)
    subject_p = _personalize(subject, _SAMPLE_VARS)
    message_p = _personalize(message, _SAMPLE_VARS)
    return {"subject": subject_p,
            "html": _update_html(subject_p, message_p, _SAMPLE_VARS["email"])}


def send_test(subject: str, message: str, to: str) -> dict:
    """Send one personalized test email to the admin — never logged as a campaign."""
    subject, message = _validate_update(subject, message)
    if not config.email_ready():
        raise ClubError("Resend is not configured — set RESEND_API_KEY first.", 503)
    to = (to or "").strip().lower()
    if not _EMAIL_RE.match(to):
        raise ClubError("No valid admin email to send the test to.")
    # Personalize with the admin's own membership if she has one, else sample data.
    with db.cursor() as c:
        row = c.execute("SELECT * FROM club_members WHERE email = ?", (to,)).fetchone()
        tags = _member_vars(c, row) if row else dict(_SAMPLE_VARS, email=to)
    subject_p = "[TEST] " + _personalize(subject, tags)
    message_p = _personalize(message, tags)
    email_send._send(to, subject_p, _update_html(subject_p, message_p, to),
                     _update_text(subject_p, message_p, to), headers=_unsub_headers(to))
    _bump_email_counter()
    return {"ok": True, "test_sent_to": to}


def resend_welcomes() -> dict:
    """Queue welcome letters for members who never received theirs (e.g. the
    Resend daily limit was hit on a busy day). Runs in the background, spaced
    under the rate limit; welcome_sent flips per member only on success, so
    this is safe to run repeatedly — nobody ever gets a second letter."""
    if not config.email_ready():
        raise ClubError("Resend is not configured — set RESEND_API_KEY first.", 503)
    with db.cursor() as c:
        rows = c.execute(
            "SELECT id, email FROM club_members WHERE welcome_sent = 0 AND unsubscribed = 0 "
            "ORDER BY id"
        ).fetchall()
    if not rows:
        return {"queued": 0}
    pending = [(r["id"], r["email"]) for r in rows]

    def run():
        for member_id, email in pending:
            with db.cursor() as c:
                row = c.execute("SELECT * FROM club_members WHERE id = ?", (member_id,)).fetchone()
                if not row or row["welcome_sent"]:
                    continue
                status = _status(c, row)
            _send_welcome(member_id, email, status)   # marks welcome_sent on success
            time.sleep(_BROADCAST_SPACING)
        print(f"[caloria][club] welcome resend pass finished ({len(pending)} attempted)")

    threading.Thread(target=run, daemon=True).start()
    return {"queued": len(pending)}


# Campaigns currently being sent by THIS process (guards double-resume).
_active_broadcasts = set()
_active_lock = threading.Lock()


def send_update(subject: str, message: str, audience: str = "all") -> dict:
    """Queue a founder update to an audience segment. Sends run in a background
    thread (spaced under Resend's rate limit); progress + a per-member watermark
    are tracked in club_updates, so an interrupted campaign can resume without
    ever emailing the same member twice."""
    subject, message = _validate_update(subject, message)
    if audience not in _AUDIENCES:
        raise ClubError("Unknown audience.")
    if not config.email_ready():
        raise ClubError("Resend is not configured — set RESEND_API_KEY first.", 503)
    total = audience_count(audience)["count"]
    if not total:
        raise ClubError("That audience has no members yet.")
    with db.cursor() as c:
        c.execute(
            "INSERT INTO club_updates (subject, body, total, status, audience) "
            "VALUES (?,?,?,'sending',?)",
            (subject, message, total, audience),
        )
        update_id = c.lastrowid
    _launch_broadcast(update_id)
    return {"id": update_id, "total": total, "status": "sending"}


def resume_update(update_id: int) -> dict:
    """Continue an interrupted campaign from its watermark (no duplicates)."""
    with db.cursor() as c:
        row = c.execute("SELECT * FROM club_updates WHERE id = ?", (update_id,)).fetchone()
    if not row:
        raise ClubError("Campaign not found.", 404)
    if row["status"] == "done":
        raise ClubError("That campaign already finished.")
    with _active_lock:
        if update_id in _active_broadcasts:
            raise ClubError("That campaign is already sending.")
    if not config.email_ready():
        raise ClubError("Resend is not configured — set RESEND_API_KEY first.", 503)
    with db.cursor() as c:
        c.execute("UPDATE club_updates SET status = 'sending' WHERE id = ?", (update_id,))
    _launch_broadcast(update_id)
    return {"id": update_id, "status": "sending"}


def _launch_broadcast(update_id: int) -> None:
    with _active_lock:
        _active_broadcasts.add(update_id)
    threading.Thread(target=_run_broadcast, args=(update_id,), daemon=True).start()


def _run_broadcast(update_id: int) -> None:
    try:
        with db.cursor() as c:
            camp = c.execute("SELECT * FROM club_updates WHERE id = ?", (update_id,)).fetchone()
        if not camp:
            return
        _label, where = _AUDIENCES.get(camp["audience"], _AUDIENCES["all"])
        kind = camp["campaign"] if "campaign" in camp.keys() else ""
        # Pre-built campaigns dedupe across button presses: skip anyone already
        # recorded as having received THIS campaign.
        dedup = (" AND email NOT IN (SELECT email FROM club_campaign_sends WHERE campaign = ?)"
                 if kind in CAMPAIGNS else "")
        dedup_args = (kind,) if dedup else ()
        sent, failed = camp["sent"], camp["failed"]
        while True:
            # One member at a time, always above the watermark — a crash or
            # restart can never produce a duplicate send.
            with db.cursor() as c:
                row = c.execute(
                    f"SELECT * FROM club_members WHERE {where}{dedup} AND id > ? "
                    "ORDER BY id LIMIT 1", (*dedup_args, camp["last_member_id"]),
                ).fetchone()
                if row:
                    tags = _member_vars(c, row)
            if not row:
                break
            if kind in CAMPAIGNS:
                # Pre-built campaign: fixed template, personalized greeting.
                with db.cursor() as c:
                    fname = _first_name(c, row["email"])
                built = CAMPAIGNS[kind]["render"](fname, row["email"])
                subject_p, html_p, text_p = built["subject"], built["html"], built["text"]
            else:
                # Free-form founder update: personalize with merge tags.
                subject_p = _personalize(camp["subject"], tags)
                message_p = _personalize(camp["body"], tags)
                html_p = _update_html(subject_p, message_p, row["email"])
                text_p = _update_text(subject_p, message_p, row["email"])
            try:
                email_send._send(row["email"], subject_p, html_p, text_p,
                                 headers=_unsub_headers(row["email"]))
                _bump_email_counter()
                sent += 1
                if kind in CAMPAIGNS:  # record so a re-press never re-sends
                    with db.cursor() as c:
                        c.execute("INSERT OR IGNORE INTO club_campaign_sends (email, campaign) "
                                  "VALUES (?, ?)", (row["email"], kind))
            except Exception as e:  # noqa: BLE001 — one bad address must not stop the run
                failed += 1
                print(f"[caloria][club] founder update to {row['email']} failed: {e}")
            camp = dict(camp, last_member_id=row["id"])
            with db.cursor() as c:
                c.execute(
                    "UPDATE club_updates SET sent = ?, failed = ?, last_member_id = ? "
                    "WHERE id = ?", (sent, failed, row["id"], update_id),
                )
            time.sleep(_BROADCAST_SPACING)
        with db.cursor() as c:
            c.execute("UPDATE club_updates SET status = 'done' WHERE id = ?", (update_id,))
        print(f"[caloria][club] founder update #{update_id} done: {sent} sent, {failed} failed")
    finally:
        with _active_lock:
            _active_broadcasts.discard(update_id)


# ---------- pre-built campaigns (Tomorrow / Early Access) ----------
def _first_name(c, email: str) -> str:
    """Personalized greeting name: the first word of the member's account name
    (waitlist rows have no name, so we cross-reference the users table by email
    and canonical email). Falls back to 'beautiful' — the intended default."""
    canon = _canonical_email(email)
    row = c.execute(
        "SELECT name FROM users WHERE lower(email) = ? OR lower(email) = ? "
        "ORDER BY id LIMIT 1", (email.lower(), canon),
    ).fetchone() if _users_table_exists(c) else None
    name = (row["name"].strip() if row and row["name"] else "")
    if name:
        first = name.split()[0]
        # Guard against an email-shaped "name".
        if "@" not in first:
            return first[:40]
    return "beautiful"


def _users_table_exists(c) -> bool:
    return bool(c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone())


_CAMPAIGN_BTN = (
    'style="display:inline-block;background:linear-gradient(135deg,#ff9ec4,#f24d8c);'
    'color:#ffffff;text-decoration:none;padding:15px 34px;border-radius:100px;'
    'font-weight:700;font-size:16px"'
)


def _campaign_shell(preheader: str, badge: str, inner: str, recipient_email: str) -> str:
    unsub = (
        f'<br><a href="{_unsubscribe_link(recipient_email)}" '
        'style="color:#b7a6b0;text-decoration:underline">Unsubscribe</a>'
        if recipient_email else ""
    )
    body = f"""\
<div style="margin:0;padding:34px 14px;background:linear-gradient(180deg,#fff6fb 0%,#fdeef6 55%,#eef9f8 100%)">
 <div style="max-width:500px;margin:0 auto;font-family:{_SANS};color:#3a2937">
  <p style="text-align:center;margin:0 0 22px;font-family:{_SERIF};font-size:25px;font-weight:700;letter-spacing:-.02em">◍ Caloria</p>
  <div style="background:#ffffff;border:1px solid #ffe6f1;border-radius:28px;padding:40px 32px;box-shadow:0 16px 44px rgba(242,77,140,.13)">
   <p style="text-align:center;margin:0 0 22px"><span style="display:inline-block;background:#fff5fa;color:#f24d8c;font-size:11px;font-weight:800;letter-spacing:.12em;text-transform:uppercase;padding:7px 16px;border-radius:100px;border:1px solid #ffd7e7">{badge}</span></p>
   {inner}
  </div>
  <p style="text-align:center;color:#b7a6b0;font-size:12px;margin:22px 0 0;line-height:1.6">Caloria Club · made with 🤍 for the first women inside.{unsub}</p>
 </div>
</div>"""
    return _email_doc(body, preheader)


def _campaign_tomorrow(first_name: str, recipient_email: str = "") -> dict:
    url = config.APP_BASE_URL.rstrip("/") + "/"   # the waitlist page
    unlocks = "".join(
        f'<tr><td style="padding:5px 0;font-size:16px;line-height:1.5">✨&nbsp; {u}</td></tr>'
        for u in ["Early Access", "Founding Member pricing",
                  "Your exclusive Founding Member badge", "Full access before the public launch"]
    )
    inner = f"""\
   <h1 style="font-family:{_SERIF};font-size:30px;font-weight:600;letter-spacing:-.015em;text-align:center;margin:0 0 24px;line-height:1.15">✨ Tomorrow.</h1>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">Hi {first_name},</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">Tomorrow is the day.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 18px">As one of our Founding Members, you'll receive your private invitation to <b>Caloria Club</b> before anyone else.</p>
   <p style="font-size:16px;line-height:1.6;margin:0 0 8px;font-weight:700">Tomorrow you'll unlock:</p>
   <table style="width:100%;border-collapse:collapse;margin:0 0 20px">{unlocks}</table>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">You're one day away.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 24px">See you tomorrow.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 26px">— Caloria Club</p>
   <p style="text-align:center;margin:0"><a href="{url}" {_CAMPAIGN_BTN}>See You Tomorrow</a></p>"""
    text = (
        f"Hi {first_name},\n\n"
        "Tomorrow is the day.\n\n"
        "As one of our Founding Members, you'll receive your private invitation to "
        "Caloria Club before anyone else.\n\n"
        "Tomorrow you'll unlock:\n"
        "  ✨ Early Access\n  ✨ Founding Member pricing\n"
        "  ✨ Your exclusive Founding Member badge\n  ✨ Full access before the public launch\n\n"
        "You're one day away.\n\nSee you tomorrow.\n\n— Caloria Club\n\n"
        f"See you tomorrow: {url}"
    )
    return {
        "subject": "✨ Tomorrow.",
        "html": _campaign_shell("Your Early Access begins tomorrow.", "✦ Caloria Club", inner, recipient_email),
        "text": text,
    }


def _campaign_early_access(first_name: str, recipient_email: str = "") -> dict:
    url = config.APP_BASE_URL.rstrip("/") + "/"   # the live website
    features = "".join(
        f'<tr><td style="padding:5px 0;font-size:16px;line-height:1.5">'
        f'<span style="color:#f24d8c">•</span>&nbsp; {f}</td></tr>'
        for f in ["AI Supermodel Coach", "AI Meal Scanner", "Personalized Meal Plans",
                  "Personalized Workout Plans", "Wellness Community",
                  "Exclusive member content", "Future updates included"]
    )
    inner = f"""\
   <h1 style="font-family:{_SERIF};font-size:29px;font-weight:600;letter-spacing:-.015em;text-align:center;margin:0 0 24px;line-height:1.2">✨ Your invitation is here.</h1>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">Hi {first_name},</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">The wait is finally over.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 18px">Your Early Access to <b>Caloria Club</b> is officially open.</p>
   <p style="font-size:16px;line-height:1.6;margin:0 0 8px;font-weight:700">Inside you'll find:</p>
   <table style="width:100%;border-collapse:collapse;margin:0 0 20px">{features}</table>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">Thank you for being one of the very first members of Caloria Club.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">I'm so excited to have you here.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 24px">See you inside.</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 26px">— Polina 🤍</p>
   <p style="text-align:center;margin:0"><a href="{url}" {_CAMPAIGN_BTN}>Enter Caloria Club</a></p>"""
    text = (
        f"Hi {first_name},\n\n"
        "The wait is finally over.\n\n"
        "Your Early Access to Caloria Club is officially open.\n\n"
        "Inside you'll find:\n"
        "  • AI Supermodel Coach\n  • AI Meal Scanner\n  • Personalized Meal Plans\n"
        "  • Personalized Workout Plans\n  • Wellness Community\n"
        "  • Exclusive member content\n  • Future updates included\n\n"
        "Thank you for being one of the very first members of Caloria Club.\n\n"
        "I'm so excited to have you here.\n\nSee you inside.\n\n— Polina 🤍\n\n"
        f"Enter Caloria Club: {url}"
    )
    return {
        "subject": "✨ Your invitation is here.",
        "html": _campaign_shell("Welcome to Caloria Club.", "✦ Early Access", inner, recipient_email),
        "text": text,
    }


def _campaign_followup(first_name: str, recipient_email: str = "") -> dict:
    url = config.APP_BASE_URL.rstrip("/") + "/"   # the Founding Members signup page
    perks = "".join(
        f'<tr><td style="padding:5px 0;font-size:16px;line-height:1.5">'
        f'<span style="color:#f24d8c">•</span>&nbsp; {p}</td></tr>'
        for p in ["Your exclusive Founding Member badge",
                  "Lifetime recognition as one of the very first members",
                  "Early access before the public launch",
                  "Founding Member perks &amp; pricing, locked in"]
    )
    inner = f"""\
   <h1 style="font-family:{_SERIF};font-size:28px;font-weight:600;letter-spacing:-.015em;text-align:center;margin:0 0 24px;line-height:1.2">Your invitation is still waiting 🤍</h1>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">Hi {first_name},</p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">A few days ago I sent you your private invitation to <b>Caloria Club</b> — but I noticed you haven't opened it yet, and I didn't want you to miss your place.</p>
   <div style="background:#fff5fa;border:1px solid #ffd7e7;border-radius:14px;padding:14px 16px;margin:0 0 18px">
     <p style="font-size:14.5px;line-height:1.6;margin:0;color:#8a5a72">💌 <b>Didn't see my first email?</b> Please check your <b>Spam</b>, <b>Promotions</b> and <b>Updates</b> folders — it sometimes hides there. Your private invitation link is inside.</p>
   </div>
   <p style="font-size:16px;line-height:1.75;margin:0 0 16px">Your Founding Member spot is <b>still reserved</b> — but only until enrollment closes.</p>
   <p style="font-size:16px;line-height:1.6;margin:0 0 8px;font-weight:700">As a Founding Member you receive:</p>
   <table style="width:100%;border-collapse:collapse;margin:0 0 18px">{perks}</table>
   <p style="font-size:16px;line-height:1.75;margin:0 0 22px;background:#fdeef6;border-radius:12px;padding:14px 16px;text-align:center;color:#a3244f;font-weight:600">⏳ Founding Members enrollment closes <b>July 15</b>.<br>After that, this opportunity is gone for good.</p>
   <p style="text-align:center;margin:0 0 26px"><a href="{url}" {_CAMPAIGN_BTN}>Join Caloria Club</a></p>
   <p style="font-size:16px;line-height:1.75;margin:0 0 6px">You were one of the first women to believe in this — I'd love for you to be one of the first inside. Don't miss your chance.</p>
   <p style="font-size:16px;line-height:1.75;margin:0">With love,<br>— Polina 🤍</p>"""
    text = (
        f"Hi {first_name},\n\n"
        "A few days ago I sent you your private invitation to Caloria Club — but I noticed "
        "you haven't opened it yet, and I didn't want you to miss your place.\n\n"
        "Didn't see my first email? Please check your Spam, Promotions and Updates folders "
        "— it sometimes hides there. Your private invitation link is inside.\n\n"
        "Your Founding Member spot is still reserved — but only until enrollment closes.\n\n"
        "As a Founding Member you receive:\n"
        "  • Your exclusive Founding Member badge\n"
        "  • Lifetime recognition as one of the very first members\n"
        "  • Early access before the public launch\n"
        "  • Founding Member perks & pricing, locked in\n\n"
        "Founding Members enrollment closes July 15. After that, this opportunity is gone for good.\n\n"
        f"Join Caloria Club: {url}\n\n"
        "You were one of the first women to believe in this — I'd love for you to be one of the "
        "first inside. Don't miss your chance.\n\nWith love,\n— Polina 🤍"
    )
    return {
        "subject": "⏳ Your private invitation is still waiting (closes July 15)",
        "html": _campaign_shell("Your Founding Member invitation closes July 15.", "✦ Founding Members", inner, recipient_email),
        "text": text,
    }


CAMPAIGNS = {
    "tomorrow": {"label": "Tomorrow", "subject": "✨ Tomorrow.", "render": _campaign_tomorrow},
    "early_access": {"label": "Early Access", "subject": "✨ Your invitation is here.", "render": _campaign_early_access},
    "followup": {"label": "Follow-up Early Access",
                 "subject": "⏳ Your private invitation is still waiting (closes July 15)",
                 "render": _campaign_followup},
}


def campaign_recipients(kind: str) -> dict:
    """Recipient breakdown for the confirmation dialog: how many will receive it,
    how many are skipped (unsubscribed), and how many already got this campaign
    (so a second press only reaches new members)."""
    if kind not in CAMPAIGNS:
        raise ClubError("Unknown campaign.")
    with db.cursor() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM club_members").fetchone()["n"]
        unsub = c.execute(
            "SELECT COUNT(*) AS n FROM club_members WHERE unsubscribed = 1"
        ).fetchone()["n"]
        # Eligible = subscribed AND not already sent this campaign.
        recipients = c.execute(
            "SELECT COUNT(*) AS n FROM club_members WHERE unsubscribed = 0 "
            "AND email NOT IN (SELECT email FROM club_campaign_sends WHERE campaign = ?)",
            (kind,),
        ).fetchone()["n"]
        already = c.execute(
            "SELECT COUNT(*) AS n FROM club_members WHERE unsubscribed = 0 "
            "AND email IN (SELECT email FROM club_campaign_sends WHERE campaign = ?)",
            (kind,),
        ).fetchone()["n"]
    return {"kind": kind, "label": CAMPAIGNS[kind]["label"], "recipients": recipients,
            "skipped": unsub + already, "already_sent": already, "unsubscribed": unsub, "total": total}


def send_campaign(kind: str) -> dict:
    """Queue a pre-built campaign (Tomorrow / Early Access) to every subscribed
    waitlist member. Reuses the resumable broadcast worker; each email is
    personalized with the member's first name."""
    if kind not in CAMPAIGNS:
        raise ClubError("Unknown campaign.")
    if not config.email_ready():
        raise ClubError("Resend is not configured — set RESEND_API_KEY first.", 503)
    info = campaign_recipients(kind)
    if not info["recipients"]:
        raise ClubError("There are no subscribed members to send to yet.")
    subject = CAMPAIGNS[kind]["subject"]
    with db.cursor() as c:
        c.execute(
            "INSERT INTO club_updates (subject, body, total, status, audience, campaign) "
            "VALUES (?,?,?,'sending','all',?)",
            (subject, "", info["recipients"], kind),
        )
        update_id = c.lastrowid
    _launch_broadcast(update_id)
    return {"id": update_id, "total": info["recipients"], "skipped": info["skipped"], "status": "sending"}


def preview_campaign(kind: str, sample_email: str = "") -> dict:
    """Render a pre-built campaign EXACTLY as members receive it (same template,
    branding, buttons) for the admin preview iframe. Sends nothing."""
    if kind not in CAMPAIGNS:
        raise ClubError("Unknown campaign.")
    to = (sample_email or "").strip().lower()
    with db.cursor() as c:
        fname = _first_name(c, to) if to else "beautiful"
    built = CAMPAIGNS[kind]["render"](fname, to or "preview@caloriaclub.com")
    return {"kind": kind, "label": CAMPAIGNS[kind]["label"],
            "subject": built["subject"], "html": built["html"]}


def test_campaign(kind: str, to: str) -> dict:
    """Send ONE copy of a pre-built campaign to the admin only — identical to what
    members get. NOT recorded in club_campaign_sends, so it never affects the
    real send's duplicate protection and can be re-sent freely."""
    if kind not in CAMPAIGNS:
        raise ClubError("Unknown campaign.")
    if not config.email_ready():
        raise ClubError("Resend is not configured — set RESEND_API_KEY first.", 503)
    to = (to or "").strip().lower()
    if not _EMAIL_RE.match(to):
        raise ClubError("No valid admin email to send the test to.")
    with db.cursor() as c:
        fname = _first_name(c, to)
    built = CAMPAIGNS[kind]["render"](fname, to)
    email_send._send(to, "[TEST] " + built["subject"], built["html"], built["text"],
                     headers=_unsub_headers(to))
    _bump_email_counter()
    return {"ok": True, "test_sent_to": to, "label": CAMPAIGNS[kind]["label"]}


# ---------- admin ----------
def overview() -> dict:
    with db.cursor() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM club_members").fetchone()["n"]
        today = c.execute(
            "SELECT COUNT(*) AS n FROM club_members WHERE created_at >= date('now')"
        ).fetchone()["n"]
        week = c.execute(
            "SELECT COUNT(*) AS n FROM club_members WHERE created_at >= date('now','-7 days')"
        ).fetchone()["n"]
        referred = c.execute(
            "SELECT COUNT(*) AS n FROM club_members WHERE referred_by IS NOT NULL"
        ).fetchone()["n"]
        top = c.execute(
            "SELECT email, referral_count FROM club_members "
            "WHERE referral_count > 0 ORDER BY referral_count DESC, id ASC LIMIT 1"
        ).fetchone()
        welcome_missing = c.execute(
            "SELECT COUNT(*) AS n FROM club_members WHERE welcome_sent = 0 AND unsubscribed = 0"
        ).fetchone()["n"]
        welcome_sent = c.execute(
            "SELECT COUNT(*) AS n FROM club_members WHERE welcome_sent = 1"
        ).fetchone()["n"]
        unsubscribed = c.execute(
            "SELECT COUNT(*) AS n FROM club_members WHERE unsubscribed = 1"
        ).fetchone()["n"]
        total_referrals = c.execute(
            "SELECT COALESCE(SUM(referral_count), 0) AS n FROM club_members"
        ).fetchone()["n"]
        # Daily growth, last 14 days (gaps filled so the sparkline is honest).
        grows = {r["d"]: r["n"] for r in c.execute(
            "SELECT date(created_at) AS d, COUNT(*) AS n FROM club_members "
            "WHERE created_at >= date('now', '-13 days') GROUP BY date(created_at)"
        ).fetchall()}
        answers = {f: _answer_breakdown(c, f) for f in _ANSWER_FIELDS}
    import datetime
    today_d = datetime.date.today()
    growth = [{"day": (today_d - datetime.timedelta(days=i)).isoformat(),
               "n": grows.get((today_d - datetime.timedelta(days=i)).isoformat(), 0)}
              for i in range(13, -1, -1)]
    return {
        "total": total,
        "welcome_missing": welcome_missing,
        "welcome_sent": welcome_sent,
        "unsubscribed": unsubscribed,
        "emails_sent_today": emails_sent_today(),
        "new_today": today,
        "new_7d": week,
        "referred": referred,
        "direct": total - referred,
        "referred_pct": round(referred / total * 100) if total else 0,
        "avg_referrals": round(total_referrals / total, 2) if total else 0,
        "growth": growth,
        "top_referrer": {"email": top["email"], "referrals": top["referral_count"]} if top else None,
        "answers": answers,
    }


def _answer_breakdown(c, field: str) -> list:
    """Answer distribution for one onboarding question (field is whitelisted)."""
    assert field in _ANSWER_FIELDS
    rows = c.execute(
        f"SELECT {field} AS answer, COUNT(*) AS count FROM club_members "
        f"WHERE {field} IS NOT NULL AND {field} != '' "
        f"GROUP BY {field} ORDER BY count DESC, answer ASC LIMIT 12"
    ).fetchall()
    return [{"answer": r["answer"], "count": r["count"]} for r in rows]


def _member_row(c, r) -> dict:
    return {
        "id": r["id"],
        "email": r["email"],
        "referral_code": r["referral_code"],
        "referral_count": r["referral_count"],
        "referred_by": r["referred_by"],
        "welcome_sent": bool(r["welcome_sent"]),
        "position": _status(c, r)["position"],
        "goal": r["goal"] or "",
        "struggle": r["struggle"] or "",
        "excited": r["excited"] or "",
        "unsubscribed": bool(r["unsubscribed"]),
        "created_at": r["created_at"],
    }


def members(q: str = "", limit: int = 200, since_days: int = 0,
            min_referrals: int = 0, invited_only: bool = False) -> list:
    """Newest members first. Filters: email substring, join-date window,
    minimum referral count, invited-only (joined through someone's link)."""
    limit = min(10000, max(1, int(limit or 200)))
    where, params = ["1=1"], []
    if q:
        where.append("email LIKE ?")
        params.append(f"%{q.strip().lower()}%")
    if since_days > 0:
        where.append("created_at >= datetime('now', ?)")
        params.append(f"-{int(since_days)} days")
    if min_referrals > 0:
        where.append("referral_count >= ?")
        params.append(int(min_referrals))
    if invited_only:
        where.append("referred_by IS NOT NULL")
    with db.cursor() as c:
        rows = c.execute(
            f"SELECT * FROM club_members WHERE {' AND '.join(where)} "
            "ORDER BY id DESC LIMIT ?", (*params, limit),
        ).fetchall()
        return [_member_row(c, r) for r in rows]


def leaderboard(limit: int = 25) -> list:
    with db.cursor() as c:
        rows = c.execute(
            "SELECT * FROM club_members ORDER BY referral_count DESC, id ASC LIMIT ?",
            (min(100, max(1, limit)),),
        ).fetchall()
        out = []
        for i, r in enumerate(rows):
            out.append({
                "rank": i + 1,
                "email": r["email"],
                "referral_code": r["referral_code"],
                "referral_count": r["referral_count"],
                "created_at": r["created_at"],
            })
        return out


def updates() -> list:
    with _active_lock:
        active = set(_active_broadcasts)
    with db.cursor() as c:
        # A campaign marked 'sending' with no live thread means the server
        # restarted mid-broadcast — surface it as resumable, never re-run silently.
        rows = c.execute("SELECT id FROM club_updates WHERE status = 'sending'").fetchall()
        stale = [r["id"] for r in rows if r["id"] not in active]
        if stale:
            c.executemany("UPDATE club_updates SET status = 'interrupted' WHERE id = ?",
                          [(i,) for i in stale])
        rows = c.execute(
            "SELECT id, subject, audience, campaign, total, sent, failed, status, created_at "
            "FROM club_updates ORDER BY id DESC LIMIT 50"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["audience_label"] = _AUDIENCES.get(d["audience"], _AUDIENCES["all"])[0]
        d["campaign_label"] = CAMPAIGNS[d["campaign"]]["label"] if d["campaign"] in CAMPAIGNS else ""
        out.append(d)
    return out
