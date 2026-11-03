"""
winter_update_email.py — the "Caloria Club Winter Update" marketing broadcast.

A one-off re-engagement campaign to the whole eligible existing-user database:
announces the Winter Update, promotes the 30-Day Winter Arc Challenge, and offers
a 3-day free Premium trial (the trial itself is pure Stripe config — this module
sends email only and NEVER touches billing).

Design goals, copied from the proven Winter Arc launch (winterarc_email.py):
  • Safe by default — nothing sends unless the campaign is ENABLED, email is
    configured, and it is triggered by the admin launch endpoint. Test mode (the
    default) restricts delivery to configured test inboxes.
  • Idempotent — a per-recipient ledger (winter_arc_sends, keyed by (user_id,
    campaign)) means a retry, a redeploy, or a second launch press never
    re-emails anyone.
  • Audience = verified + active + not-unsubscribed accounts. The SAME one-click
    product-announcement unsubscribe as the Winter Arc launch is honoured (it sets
    users.broadcast_unsubscribed=1), so opting out of one opts out of both.
  • Billing-safe — reads the users table only; writes nothing to users/billing.

The exact marketing copy is fixed by the founder and must not be reworded; only
its visual presentation (layout, emphasis, colour) lives here.
"""
from __future__ import annotations

import time

import config
import db
import email_send
import winterarc_email  # reuse the existing broadcast unsubscribe (same opt-out)

# The founder's sign-off name. NOTE: spelled with a double "l" to match the live
# sending identity ("Paullina from Caloria <hello@caloriaclub.com>"). Change here
# only — it is the single source of the in-body signature.
SIGNATURE_NAME = "Paullina Ruban"

_SUBJECT = "Tap here to unlock your dream body 💗"
_PREHEADER = "Your Winter Update is here — plus 3 days free of Caloria Premium ❄️"

_BATCH_PAUSE = 0.05  # gentle pacing between sends (Resend reputation guard)


class CampaignError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


# --------------------------------------------------------------------------- #
# CTA target
# --------------------------------------------------------------------------- #
def _cta_url() -> str:
    """Where the button sends people: the live site, which opens the paywall /
    3-day-trial checkout. UTM tags are for attribution only — the app reads just
    the `checkout` param, so these never change site behaviour."""
    base = config.APP_BASE_URL.rstrip("/")
    return (f"{base}/?utm_source=email&utm_medium=broadcast"
            f"&utm_campaign={config.WINTER_UPDATE_CAMPAIGN_ID}")


# --------------------------------------------------------------------------- #
# email content — powder pink + white, table-based, iPhone-friendly
# --------------------------------------------------------------------------- #
_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light">
<title>Caloria Club Winter Update</title>
<style>
  @media only screen and (max-width:600px){
    .wrap{width:100%!important}
    .pad{padding-left:24px!important;padding-right:24px!important}
    .h1{font-size:28px!important;line-height:1.2!important}
    .free{font-size:34px!important}
    .cta a{display:block!important}
  }
</style>
</head>
<body style="margin:0;padding:0;background:#fdeef3;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#fdeef3;font-size:1px;line-height:1px;">__PREHEADER__</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#fdeef3;">
  <tr><td align="center" style="padding:32px 16px;">
    <table role="presentation" class="wrap" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;">

      <!-- wordmark -->
      <tr><td align="center" style="padding:4px 0 22px;">
        <span style="font-family:Georgia,'Times New Roman',serif;font-size:15px;letter-spacing:4px;color:#e86aa0;font-weight:700;">CALORIA&nbsp;CLUB</span>
      </td></tr>

      <!-- card -->
      <tr><td class="pad" style="background:#ffffff;border-radius:22px;padding:44px 48px;box-shadow:0 10px 30px rgba(232,106,160,0.10);">

        <!-- snow accent -->
        <div style="text-align:center;font-size:20px;color:#f1a9c6;letter-spacing:8px;margin:0 0 18px;">❄&nbsp;❅&nbsp;❄</div>

        <!-- hero headline (exact copy) -->
        <h1 class="h1" style="margin:0 0 18px;font-family:Georgia,'Times New Roman',serif;font-size:33px;line-height:1.18;color:#3a2730;text-align:center;font-weight:700;">
          ❄️ Caloria Club <span style="color:#e86aa0;">Winter Update</span> is here! ❄️
        </h1>

        <!-- subhead (exact copy) -->
        <p style="margin:0 0 26px;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:17px;line-height:1.5;color:#8a7886;text-align:center;">
          <b style="color:#5a4551;">30 days.</b> New habits. A new version of you.
        </p>

        <!-- body (exact copy) -->
        <p style="margin:0 0 22px;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:16px;line-height:1.65;color:#4a3540;text-align:center;">
          Just <b>30 days</b> to build habits, create your routine and get closer to your
          dream body with the new <b style="color:#e86aa0;">Winter Arc Challenge</b>.
        </p>

        <!-- connector (exact copy) -->
        <p style="margin:0 0 20px;font-family:Georgia,'Times New Roman',serif;font-style:italic;font-size:17px;color:#b78aa0;text-align:center;">
          And there&rsquo;s more&hellip;
        </p>

        <!-- 3 DAYS FREE emphasis block (exact copy) -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 30px;">
          <tr><td align="center" style="background:#fbe3ec;border-radius:16px;padding:26px 20px;">
            <div style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:14px;letter-spacing:2px;text-transform:uppercase;color:#c04a78;margin:0 0 6px;">Get</div>
            <div class="free" style="font-family:Georgia,'Times New Roman',serif;font-size:40px;line-height:1;color:#d84f87;font-weight:700;margin:0 0 8px;">3 DAYS FREE</div>
            <div style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:16px;color:#7a5e6b;">to experience <b>Caloria Premium</b>!</div>
          </td></tr>
        </table>

        <!-- start building today (exact copy) -->
        <p style="margin:0 0 26px;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:17px;line-height:1.5;color:#4a3540;text-align:center;font-weight:600;">
          Start building today.
        </p>

        <!-- CTA (bulletproof, powder pink) -->
        <table role="presentation" class="cta" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td align="center" style="padding:2px 0 8px;">
            <a href="__CTA_URL__" style="background:#ec6e9c;color:#ffffff;text-decoration:none;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:17px;font-weight:700;letter-spacing:.5px;padding:17px 40px;border-radius:100px;display:inline-block;">
              UNLOCK MY 3 DAYS FREE
            </a>
          </td></tr>
        </table>

        <!-- hairline -->
        <div style="border-top:1px solid #f3dde6;margin:34px 0 22px;"></div>

        <!-- signature (exact copy) -->
        <p style="margin:0;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:16px;line-height:1.6;color:#4a3540;">
          With love,<br>
          <b style="font-family:Georgia,'Times New Roman',serif;font-size:18px;color:#3a2730;">__SIGNATURE__</b><br>
          <span style="color:#8a7886;font-size:14px;">Founder of Caloria Club 💗</span>
        </p>

      </td></tr>

      <!-- footer -->
      <tr><td class="pad" style="padding:26px 40px 8px;text-align:center;">
        <p style="margin:0 0 8px;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:12px;line-height:1.6;color:#b7a2ac;">
          You&rsquo;re receiving this because you have a Caloria Club account.
        </p>
        <p style="margin:0;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;font-size:12px;line-height:1.6;color:#b7a2ac;">
          <a href="__UNSUB_URL__" style="color:#b7a2ac;text-decoration:underline;">Unsubscribe from announcements</a>
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Version B — minimal / personal (text-first, tuned for the Primary tab)
# Same offer, CTA, sender, signature and unsubscribe; just a quieter, more
# conversational presentation with almost no promotional formatting.
# --------------------------------------------------------------------------- #
_PREHEADER_MIN = "The Winter Update is here — and 3 days of Premium on me."

_HTML_TEMPLATE_MIN = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
</head>
<body style="margin:0;padding:0;background:#ffffff;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#ffffff;font-size:1px;line-height:1px;">__PREHEADER__</div>
<div style="font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;max-width:524px;margin:0 auto;padding:30px 22px;color:#2b2b2b;font-size:16px;line-height:1.62;">
  <p style="margin:0 0 20px;font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#b48aa0;">Caloria Club</p>
  <p style="margin:0 0 16px;">Hi there,</p>
  <p style="margin:0 0 16px;">I wanted to share something I&rsquo;ve been working on &mdash; the <b>Caloria Club Winter Update</b> is here, and with it the new <b>30-Day Winter Arc Challenge</b>. &#10052;&#65039;</p>
  <p style="margin:0 0 16px;">It&rsquo;s a simple idea: 30 days to build small habits, create your routine, and get a little closer to your dream body &mdash; one day at a time.</p>
  <p style="margin:0 0 16px;">And I&rsquo;d love for you to try it with everything unlocked, so here are <b>3 days free</b> of Caloria Premium, on me.</p>
  <p style="margin:0 0 22px;">If you&rsquo;ve been meaning to come back, this is a lovely place to start.</p>
  <p style="margin:0 0 26px;"><a href="__CTA_URL__" style="color:#d84f87;font-weight:600;">Start my 3 days free &rarr;</a></p>
  <p style="margin:0 0 2px;">With love,</p>
  <p style="margin:0 0 2px;">__SIGNATURE__</p>
  <p style="margin:0;color:#777;">Founder of Caloria Club &#128151;</p>
  <div style="border-top:1px solid #eeeeee;margin:28px 0 12px;"></div>
  <p style="margin:0;color:#9a9a9a;font-size:12px;line-height:1.5;">You&rsquo;re receiving this because you have a Caloria Club account. <a href="__UNSUB_URL__" style="color:#9a9a9a;">Unsubscribe</a>.</p>
</div>
</body>
</html>
"""


def _render(to_name: str, user_id: int, variant: str = "a", subject: str = None):
    """Return (subject, html, text) for one recipient.

    variant="a" → the designed, powder-pink broadcast (default).
    variant="b" → the minimal, personal, text-first version (Primary-tab test).
    `subject` overrides the subject line (used for subject A/B experiments).
    `to_name` is accepted for future personalisation; the copy has no name
    greeting, so it is intentionally unused in the body."""
    cta = _cta_url()
    unsub = winterarc_email.unsub_url(user_id)
    subj = subject or _SUBJECT

    if variant == "b":
        html = (_HTML_TEMPLATE_MIN
                .replace("__PREHEADER__", _PREHEADER_MIN)
                .replace("__CTA_URL__", cta)
                .replace("__UNSUB_URL__", unsub)
                .replace("__SIGNATURE__", SIGNATURE_NAME))
        text = (
            "Hi there,\n\n"
            "I wanted to share something I've been working on - the Caloria Club "
            "Winter Update is here, and with it the new 30-Day Winter Arc Challenge.\n\n"
            "It's a simple idea: 30 days to build small habits, create your routine, "
            "and get a little closer to your dream body - one day at a time.\n\n"
            "And I'd love for you to try it with everything unlocked, so here are "
            "3 days free of Caloria Premium, on me.\n\n"
            "If you've been meaning to come back, this is a lovely place to start.\n\n"
            f"Start my 3 days free: {cta}\n\n"
            f"With love,\n{SIGNATURE_NAME}\nFounder of Caloria Club\n\n"
            "You're receiving this because you have a Caloria Club account.\n"
            f"Unsubscribe: {unsub}"
        )
        return subj, html, text

    html = (_HTML_TEMPLATE
            .replace("__PREHEADER__", _PREHEADER)
            .replace("__CTA_URL__", cta)
            .replace("__UNSUB_URL__", unsub)
            .replace("__SIGNATURE__", SIGNATURE_NAME))
    text = (
        "CALORIA CLUB WINTER UPDATE IS HERE!\n\n"
        "30 days. New habits. A new version of you.\n\n"
        "Just 30 days to build habits, create your routine and get closer to your "
        "dream body with the new Winter Arc Challenge.\n\n"
        "And there's more...\n\n"
        "Get 3 DAYS FREE to experience Caloria Premium!\n\n"
        "Start building today.\n\n"
        f"Unlock your 3 days free: {cta}\n\n"
        f"With love,\n{SIGNATURE_NAME}\nFounder of Caloria Club\n\n"
        f"Unsubscribe from announcements: {unsub}"
    )
    return subj, html, text


# --------------------------------------------------------------------------- #
# audience + idempotency (identical eligibility to the Winter Arc launch)
# --------------------------------------------------------------------------- #
# Obvious throwaway / internal test domains, excluded from the marketing audience.
_TEST_DOMAINS = ("@test.com", "@t.com", "@example.com", "@example.org",
                 "@test.test", "@mailinator.com")


def _is_test_account(email: str) -> bool:
    e = (email or "").lower().strip()
    if e in config.ADMIN_EMAILS:
        return True
    return any(e.endswith(d) for d in _TEST_DOMAINS)


def _eligible_rows(exclude_subscribers: bool = False):
    """Verified + active + not-unsubscribed accounts, de-duplicated by email, with
    admin/test accounts removed. Optionally also excludes currently-active
    subscribers (to whom a '3 days free' offer does not apply)."""
    sub_clause = ""
    if exclude_subscribers:
        # plan/subscription_status reflect the Stripe webhook's cached state.
        sub_clause = ("AND NOT (COALESCE(plan,'free')<>'free' "
                      "AND COALESCE(subscription_status,'') IN ('active','trialing','past_due')) ")
    with db.cursor() as c:
        rows = c.execute(
            "SELECT id, email, name FROM users "
            "WHERE email IS NOT NULL AND TRIM(email) <> '' "
            "AND email_verified=1 AND active=1 "
            "AND COALESCE(broadcast_unsubscribed,0)=0 "
            + sub_clause +
            "GROUP BY LOWER(TRIM(email)) "   # de-dupe by normalised email
            "ORDER BY id ASC",
        ).fetchall()
    # Drop admin + obvious test accounts (ADMIN_EMAILS is a Python set).
    return [dict(r) for r in rows if not _is_test_account(r["email"])]


def _recipients(test_mode: bool, exclude_subscribers: bool = False) -> list:
    recips = _eligible_rows(exclude_subscribers)
    if test_mode:
        recips = [r for r in recips
                  if (r["email"] or "").lower() in config.WINTER_UPDATE_TEST_EMAILS]
    return recips


def audience_breakdown() -> dict:
    """Read-only. Returns eligible + excluded counts with reasons. Sends nothing.
    Run this against PRODUCTION to get the real numbers for the final report."""
    with db.cursor() as c:
        def n(where, *a):
            return c.execute(f"SELECT COUNT(*) FROM users WHERE {where}", a).fetchone()[0]
        total = n("1=1")
        with_email = n("email IS NOT NULL AND TRIM(email) <> ''")
        no_email = total - with_email
        unverified = n("email_verified=0 OR email_verified IS NULL")
        inactive = n("active=0")
        unsubscribed = n("COALESCE(broadcast_unsubscribed,0)=1")
        active_subs = n("COALESCE(plan,'free')<>'free' AND "
                        "COALESCE(subscription_status,'') IN ('active','trialing','past_due')")
    # The real send size (these use the SAME filter as the actual send, including
    # de-dupe by normalised email and removal of admin/test accounts).
    eligible = len(_eligible_rows(exclude_subscribers=False))
    eligible_excl_subs = len(_eligible_rows(exclude_subscribers=True))
    return {
        "total_registered_users": total,
        "eligible_recipients": eligible,
        "eligible_excluding_active_subscribers": eligible_excl_subs,
        "excluded": {
            "no_email": no_email,
            "unverified_email": unverified,
            "deactivated_account": inactive,
            "unsubscribed_from_announcements": unsubscribed,
            "currently_active_subscribers": active_subs,
            "admin_or_test_accounts": "removed from eligible list above",
        },
        "note": ("Hard-bounced / spam-complaint addresses are suppressed by Resend "
                 "automatically and are not re-sent even if they appear eligible here."),
    }


def _already_sent(user_id: int, campaign: str) -> bool:
    with db.cursor() as c:
        return c.execute(
            "SELECT 1 FROM winter_arc_sends WHERE user_id=? AND campaign=? AND status='sent'",
            (user_id, campaign),
        ).fetchone() is not None


# --------------------------------------------------------------------------- #
# admin test send (one copy to an explicit address; NOT recorded in the ledger)
# --------------------------------------------------------------------------- #
def send_test(to_email: str, user_id_for_links: int = 0,
              variant: str = "a", subject: str = None) -> dict:
    """Send exactly one copy to `to_email` so the founder can preview delivery.
    Never recorded in the campaign ledger, so it can be re-sent freely and does
    not affect the real audience or dedupe. `variant` picks the designed ("a") or
    minimal/personal ("b") version; `subject` overrides the subject line."""
    if not config.email_ready():
        raise CampaignError("Email is not configured (RESEND_API_KEY missing).", 503)
    to_email = (to_email or "").strip()
    if "@" not in to_email:
        raise CampaignError("A valid test recipient address is required.", 400)
    subject, html, text = _render("", user_id_for_links or 0, variant=variant, subject=subject)
    mid = email_send._send(
        to_email, subject, html, text,
        headers={"List-Unsubscribe": f"<{winterarc_email.unsub_url(user_id_for_links or 0)}>",
                 "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
        category="marketing",
        idempotency_key=f"winterupdate-test:{variant}:{to_email}:{int(time.time()//3600)}",
    )
    return {"ok": True, "to": to_email, "variant": variant, "subject": subject,
            "message_id": mid,
            "note": "Resend accepted the message." if mid else
                    "Dev console fallback (no RESEND_API_KEY) — not actually sent."}


# --------------------------------------------------------------------------- #
# the real broadcast (gated, idempotent, resumable) — mirrors winterarc_email
# --------------------------------------------------------------------------- #
def launch_campaign(test_mode: bool = True, limit: int = None,
                    exclude_subscribers: bool = False, variant: str = "a") -> dict:
    if not config.WINTER_UPDATE_ENABLED:
        raise CampaignError(
            "Winter Update campaign is not enabled — set WINTER_UPDATE_ENABLED=true "
            "to allow sending.", 409)
    if not config.email_ready():
        raise CampaignError("Email is not configured (RESEND_API_KEY missing).", 503)

    campaign = config.WINTER_UPDATE_CAMPAIGN_ID + ("-test" if test_mode else "")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db.cursor() as c:
        c.execute(
            "INSERT INTO winter_arc_campaign (campaign, status, test_mode, started_at) "
            "VALUES (?, 'sending', ?, ?) "
            "ON CONFLICT(campaign) DO UPDATE SET status='sending', started_at=COALESCE(started_at, ?)",
            (campaign, 1 if test_mode else 0, now, now),
        )

    recips = _recipients(test_mode, exclude_subscribers)
    sent = failed = skipped = 0
    for r in recips:
        if limit and (sent + failed) >= limit:
            break
        uid = r["id"]
        if _already_sent(uid, campaign):
            skipped += 1
            continue
        subject, html, text = _render(r["name"], uid, variant=variant)
        headers = {"List-Unsubscribe": f"<{winterarc_email.unsub_url(uid)}>",
                   "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
        try:
            email_send._send(r["email"], subject, html, text, headers=headers,
                             category="marketing",
                             idempotency_key=f"winterupdate:{campaign}:{uid}")
            status = "sent"; sent += 1
            print(f"[caloria][winterupdate] sent user={uid}")
        except Exception as e:  # noqa: BLE001 — one failure must not stop the batch
            status = "failed"; failed += 1
            print(f"[caloria][winterupdate] failed user={uid}: {e}")
        with db.cursor() as c:
            c.execute(
                "INSERT OR REPLACE INTO winter_arc_sends (user_id, campaign, status, sent_at) "
                "VALUES (?,?,?,?)",
                (uid, campaign, status, now),
            )
            c.execute(
                "UPDATE winter_arc_campaign SET total=?, sent=?, failed=?, skipped=? WHERE campaign=?",
                (len(recips), sent, failed, skipped, campaign),
            )
        if _BATCH_PAUSE:
            time.sleep(_BATCH_PAUSE)

    done = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db.cursor() as c:
        c.execute("UPDATE winter_arc_campaign SET status='done', finished_at=? WHERE campaign=?",
                  (done, campaign))
    print(f"[caloria][winterupdate] campaign={campaign} test={test_mode} "
          f"total={len(recips)} sent={sent} failed={failed} skipped={skipped}")
    return {"campaign": campaign, "test_mode": test_mode, "eligible": len(recips),
            "sent": sent, "failed": failed, "skipped": skipped, "status": "done"}
