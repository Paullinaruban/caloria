"""
winterarc_email.py — the Winter Arc launch announcement campaign.

Safe, intentional, idempotent. Reuses the existing Resend sender (email_send)
and the existing "verified + active" audience, adds a product-announcement
unsubscribe, and records a per-recipient ledger so a retry (or a second launch
press) never re-emails anyone. The actual production send is gated: the seasonal
feature must be enabled, email must be configured, and it must be triggered by
the admin launch endpoint. Test mode (the default) sends only to configured test
recipients so nothing goes out during development.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

import config
import db
import email_send

# Unsubscribe-token signing key. Dedicated env var, with a stable fallback so
# dev works; the token only toggles a marketing opt-out, never grants access.
_UNSUB_SECRET = (os.environ.get("UNSUB_SECRET")
                 or config.APP_CLIENT_SECRET
                 or "caloria-winter-arc-unsub").encode()
# The API's own public origin (where the unsubscribe endpoint lives). Falls back
# to APP_BASE_URL; set CALORIA_API_PUBLIC in prod for correct links.
_API_BASE = (os.environ.get("CALORIA_API_PUBLIC") or config.APP_BASE_URL).rstrip("/")
_SITE_BASE = config.APP_BASE_URL.rstrip("/")

_BATCH_PAUSE = 0.05   # gentle pacing between sends


class CampaignError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message); self.message = message; self.code = code


# --------------------------------------------------------------------------- #
# unsubscribe
# --------------------------------------------------------------------------- #
def unsub_token(user_id: int) -> str:
    sig = hmac.new(_UNSUB_SECRET, f"unsub:{user_id}".encode(), hashlib.sha256).hexdigest()[:24]
    raw = f"{user_id}.{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _verify_token(token: str):
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode((token + pad).encode()).decode()
        uid_s, sig = raw.split(".", 1)
        expected = hmac.new(_UNSUB_SECRET, f"unsub:{int(uid_s)}".encode(),
                            hashlib.sha256).hexdigest()[:24]
        if hmac.compare_digest(sig, expected):
            return int(uid_s)
    except Exception:
        pass
    return None


def unsubscribe(token: str) -> bool:
    uid = _verify_token(token)
    if uid is None:
        return False
    with db.cursor() as c:
        c.execute("UPDATE users SET broadcast_unsubscribed = 1 WHERE id = ?", (uid,))
    print(f"[caloria][winter] user={uid} unsubscribed from product announcements")
    return True


def unsub_url(user_id: int) -> str:
    return f"{_API_BASE}/api/winterarc/unsubscribe?t={unsub_token(user_id)}"


def unsubscribe_page(ok: bool) -> str:
    msg = ("You're unsubscribed from Caloria product announcements 💗"
           if ok else "That unsubscribe link looks invalid or expired.")
    return (f'<!doctype html><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1">'
            f'<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:420px;'
            f'margin:64px auto;padding:28px;text-align:center;color:#3a2937">'
            f'<h1 style="color:#f24d8c;font-size:22px">Caloria</h1><p>{msg}</p></div>')


# --------------------------------------------------------------------------- #
# email content (brand-matched, reuses email_send helpers)
# --------------------------------------------------------------------------- #
def _render(to_name: str, user_id: int):
    cta = f"{_SITE_BASE}/?winterarc=1"
    body = (
        f"<p>Hey {to_name or 'girl'},</p>"
        "<p>Your <b>Winter Arc</b> is officially here! We've added a festive new "
        "experience to Caloria Club, and you're invited to make the next 30 days your own.</p>"
        "<p>✨ Discover your 30-day Winter Arc calendar.<br>"
        "🎁 Complete daily wellness challenges at your own pace.<br>"
        "❄️ Unlock seasonal badges and celebrate your progress.</p>"
        "<p>Your Winter Arc isn't about perfection. It's about building little habits "
        "that make you feel good.</p>"
        + email_send._button(cta, "START MY WINTER ARC")
        + "<p style='color:#8a7886'>See you inside Caloria Club,<br>The Caloria Club Team</p>"
        + f"<p style='color:#b7a9b2;font-size:11px;margin-top:22px'>"
          f"You're receiving this because you have a verified Caloria account. "
          f"<a href='{unsub_url(user_id)}' style='color:#b7a9b2'>Unsubscribe from announcements</a>.</p>"
    )
    html = email_send._shell("Your Winter Arc just dropped ❄️", body)
    text = ("Your Winter Arc is here! A festive 30-day wellness journey inside Caloria Club.\n"
            f"Start: {cta}\nUnsubscribe: {unsub_url(user_id)}")
    return html, text


# --------------------------------------------------------------------------- #
# audience + ledger
# --------------------------------------------------------------------------- #
def _recipients(test_mode: bool, after_id: int = 0) -> list:
    """Eligible users: verified, active, not unsubscribed. Test mode restricts to
    the configured test addresses so nothing escapes during development."""
    with db.cursor() as c:
        rows = c.execute(
            "SELECT id, email, name FROM users "
            "WHERE email_verified=1 AND active=1 AND COALESCE(broadcast_unsubscribed,0)=0 "
            "AND id > ? ORDER BY id ASC",
            (after_id,),
        ).fetchall()
    recips = [dict(r) for r in rows]
    if test_mode:
        recips = [r for r in recips if (r["email"] or "").lower() in config.WINTER_ARC_TEST_EMAILS]
    return recips


def _already_sent(user_id: int, campaign: str) -> bool:
    # Only a SUCCESSFUL prior send blocks a resend — a prior failure is retried.
    with db.cursor() as c:
        return c.execute(
            "SELECT 1 FROM winter_arc_sends WHERE user_id=? AND campaign=? AND status='sent'",
            (user_id, campaign),
        ).fetchone() is not None


def launch_campaign(test_mode: bool = True, limit: int = None) -> dict:
    """Send the launch announcement. Idempotent per (user, campaign).

    Guards (so it can't fire accidentally or before the feature is live):
      - the seasonal feature must be ENABLED (WINTER_ARC_ENABLED);
      - email must be configured;
      - in non-test mode, test recipients are not required, but the ledger still
        guarantees no duplicates.
    """
    if not config.WINTER_ARC_ENABLED:
        raise CampaignError("Winter Arc is not enabled — refusing to announce a feature that is off.", 409)
    if not config.email_ready():
        raise CampaignError("Email is not configured (RESEND_API_KEY missing).", 503)

    # Test sends use a separate ledger id so they never block the real campaign.
    campaign = config.WINTER_ARC_CAMPAIGN_ID + ("-test" if test_mode else "")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with db.cursor() as c:
        c.execute(
            "INSERT INTO winter_arc_campaign (campaign, status, test_mode, started_at) "
            "VALUES (?, 'sending', ?, ?) "
            "ON CONFLICT(campaign) DO UPDATE SET status='sending', started_at=COALESCE(started_at, ?)",
            (campaign, 1 if test_mode else 0, now, now),
        )

    recips = _recipients(test_mode)
    sent = failed = skipped = 0
    for i, r in enumerate(recips):
        if limit and (sent + failed) >= limit:
            break
        uid = r["id"]
        if _already_sent(uid, campaign):
            skipped += 1
            continue
        html, text = _render(r["name"], uid)
        headers = {"List-Unsubscribe": f"<{unsub_url(uid)}>",
                   "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
        try:
            email_send._send(r["email"], "Your Winter Arc just dropped ❄️", html, text,
                             headers=headers,
                             idempotency_key=f"winterarc:{campaign}:{uid}")
            status = "sent"; sent += 1
            print(f"[caloria][winter] winter_arc_email_sent user={uid}")
        except Exception as e:  # noqa: BLE001 — one failure must not stop the batch
            status = "failed"; failed += 1
            print(f"[caloria][winter] winter_arc_email_failed user={uid}: {e}")
        with db.cursor() as c:
            # OR REPLACE so a prior 'failed' row flips to 'sent' on a successful retry.
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
    print(f"[caloria][winter] winter_arc_email_campaign_started campaign={campaign} "
          f"test={test_mode} total={len(recips)} sent={sent} failed={failed} skipped={skipped}")
    return {"campaign": campaign, "test_mode": test_mode, "eligible": len(recips),
            "sent": sent, "failed": failed, "skipped": skipped, "status": "done"}
