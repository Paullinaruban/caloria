"""Automated recovery & conversion emails — safe, consent-aware, idempotent.

Three nudges for people who dropped out of the funnel, each tied to a DISTINCT,
independently-tracked stage (so we never mislabel anyone):

  • Email A — EMAIL CAPTURED, NO ACCOUNT  (marketing / re-engagement)
      A prospect left their email in the popup but never created an account.
      This is a marketing email: it is sent ONLY with explicit opt-in
      (leads.marketing_consent=1), honours one-click unsubscribe, and stops the
      moment they register.

  • Email B — ABANDONED CHECKOUT  (transactional)
      A registered user asked us to open a Stripe Checkout Session but never
      completed it. "Completed" is read from the LIVE users row (the Stripe
      webhook maintains plan / subscription_status), so billing.py is untouched.
      A user who has verified their email but abandoned checkout is an abandoned
      checkout — NOT an incomplete registration.

  • Email C — INCOMPLETE REGISTRATION  (transactional)
      A user created an account but has not verified their email, and has NOT
      started checkout. (If they started checkout, they are an Email-B case.)

Design notes that make this safe:
  • There is NO queue of pre-scheduled sends. Every tick recomputes candidates
    from live state, so a completed step is suppressed on the very next pass —
    there is nothing stale to cancel.
  • Every send is claimed in recovery_email_sends BEFORE Resend is called, so a
    retry, a redeploy, or an overlapping tick can never double-email anyone.
  • The daemon only auto-sends when config.recovery_ready() (RECOVERY_EMAILS_ENABLED
    AND a Resend key). While testing, RECOVERY_TEST_EMAILS restricts delivery to
    your own inbox so nothing can escape.
  • Conversion (plan != free, or an active/trialing/past_due subscription) and a
    broadcast/marketing opt-out suppress every email, re-checked at send time.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import os
import time

import config
import db
import email_send
import leads

# A subscription in any of these states means the person is (or recently was) a
# paying customer — never send them a "come finish paying" nudge.
_ACTIVE_STATUSES = {"active", "trialing", "past_due"}

# Unsubscribe-token signing key (dedicated env var, stable dev fallback). The
# token only toggles a marketing opt-out — it never grants access to anything.
_UNSUB_SECRET = (os.environ.get("RECOVERY_UNSUB_SECRET")
                 or os.environ.get("UNSUB_SECRET")
                 or config.APP_CLIENT_SECRET
                 or "caloria-recovery-unsub").encode()
_SITE_BASE = config.APP_BASE_URL.rstrip("/")
_API_BASE = (os.environ.get("CALORIA_API_PUBLIC") or config.APP_BASE_URL).rstrip("/")


# --------------------------------------------------------------------------- #
# time helpers (all UTC, matching SQLite CURRENT_TIMESTAMP: 'YYYY-MM-DD HH:MM:SS')
# --------------------------------------------------------------------------- #
def _ago(minutes: float) -> str:
    t = datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# one-click unsubscribe (marketing)
# --------------------------------------------------------------------------- #
def unsub_token(ref: str) -> str:
    sig = hmac.new(_UNSUB_SECRET, f"unsub:{ref}".encode(), hashlib.sha256).hexdigest()[:24]
    raw = f"{ref}.{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _verify_token(token: str):
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode((token + pad).encode()).decode()
        ref, sig = raw.rsplit(".", 1)
        expected = hmac.new(_UNSUB_SECRET, f"unsub:{ref}".encode(),
                            hashlib.sha256).hexdigest()[:24]
        if hmac.compare_digest(sig, expected):
            return ref
    except Exception:  # noqa: BLE001
        pass
    return None


def unsub_url(ref: str) -> str:
    return f"{_API_BASE}/api/recovery/unsubscribe?t={unsub_token(ref)}"


def unsubscribe(token: str) -> bool:
    """Honour a one-click opt-out. 'user:<id>' refs flip the account's broadcast
    flag; a bare lead id flips the lead's marketing opt-out."""
    ref = _verify_token(token)
    if ref is None:
        return False
    if ref.startswith("user:"):
        try:
            uid = int(ref.split(":", 1)[1])
        except (ValueError, IndexError):
            return False
        with db.cursor() as c:
            c.execute("UPDATE users SET broadcast_unsubscribed = 1 WHERE id = ?", (uid,))
        print(f"[caloria][recovery] user={uid} unsubscribed from recovery emails")
        return True
    ok = leads.set_unsubscribed_by_id(ref)
    if ok:
        print(f"[caloria][recovery] lead unsubscribed from marketing emails")
    return ok


def unsubscribe_page(ok: bool) -> str:
    msg = ("You're unsubscribed — we won't send you these reminders again 💗"
           if ok else "That unsubscribe link looks invalid or expired.")
    return (f'<!doctype html><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1">'
            f'<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:420px;'
            f'margin:64px auto;padding:28px;text-align:center;color:#3a2937">'
            f'<h1 style="color:#f24d8c;font-size:22px">Caloria</h1><p>{msg}</p></div>')


# --------------------------------------------------------------------------- #
# funnel-event hooks (called best-effort from the request handlers)
# --------------------------------------------------------------------------- #
def record_checkout_started(user_id: int, interval: str = "") -> None:
    """A user asked the server to open a Checkout Session. One open attempt per
    user; a fresh attempt re-opens the window and clears any stale completion."""
    if not user_id:
        return
    with db.cursor() as c:
        c.execute(
            "INSERT INTO checkout_attempts (user_id, interval, started_at, completed_at) "
            "VALUES (?,?,CURRENT_TIMESTAMP,NULL) "
            "ON CONFLICT(user_id) DO UPDATE SET interval=excluded.interval, "
            "started_at=CURRENT_TIMESTAMP, completed_at=NULL",
            (int(user_id), interval or None),
        )


def mark_checkout_completed(user_id: int) -> None:
    """Best-effort bookkeeping when a checkout converts. Suppression does not
    depend on this (it reads the live users row), but it keeps the admin view
    and abandoned-checkout queries clean."""
    if not user_id:
        return
    with db.cursor() as c:
        c.execute(
            "UPDATE checkout_attempts SET completed_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND completed_at IS NULL",
            (int(user_id),),
        )


def on_user_registered(email: str, user_id: int) -> None:
    """A lead became an account → stop the marketing email immediately."""
    leads.link_user(email, user_id)


def on_email_verified(email: str) -> None:
    """An account verified its email → Email C no longer applies (bookkeeping;
    live suppression already reads users.email_verified)."""
    leads.mark_email_verified(email)


# --------------------------------------------------------------------------- #
# idempotent ledger
# --------------------------------------------------------------------------- #
def _already_sent(ref: str, email_type: str) -> bool:
    with db.cursor() as c:
        return c.execute(
            "SELECT 1 FROM recovery_email_sends WHERE ref=? AND email_type=? AND status='sent'",
            (ref, email_type),
        ).fetchone() is not None


def _record(ref: str, email_type: str, status: str) -> None:
    with db.cursor() as c:
        c.execute(
            "INSERT OR REPLACE INTO recovery_email_sends (ref, email_type, status, sent_at) "
            "VALUES (?,?,?,CURRENT_TIMESTAMP)",
            (ref, email_type, status),
        )


def _test_ok(to: str) -> bool:
    """In test mode (any RECOVERY_TEST_EMAILS set) only those addresses receive
    mail, so a real prospect can never be emailed before production is approved."""
    if config.RECOVERY_TEST_EMAILS:
        return (to or "").lower() in config.RECOVERY_TEST_EMAILS
    return True


def _converted(row) -> bool:
    """True if this account is (or recently was) a paying customer."""
    plan = (row["plan"] if "plan" in row.keys() else None) or "free"
    status = (row["subscription_status"] if "subscription_status" in row.keys() else None) or ""
    return plan != "free" or status in _ACTIVE_STATUSES


# --------------------------------------------------------------------------- #
# email bodies (reuse the established transactional shell → good deliverability)
# --------------------------------------------------------------------------- #
def _card(inner: str) -> str:
    # Soft champagne/cream panel on the existing pink-accented shell.
    return (f'<div style="background:#fff7f1;border:1px solid #f3e7dc;border-radius:14px;'
            f'padding:16px 18px;margin:14px 0;color:#5a4a52">{inner}</div>')


def _email_a(name: str, lead_ref: str):
    cta = f"{_SITE_BASE}/?resume={lead_ref}"
    unsub = unsub_url(lead_ref)
    body = (
        f"<p>Hi {name or 'there'},</p>"
        "<p>You're one small step from your glow-up. Your personalized Caloria Club "
        "plan is ready whenever you are — and your <b>3 days are completely free</b>.</p>"
        + _card("✨ AI meal scanning<br>💬 Your AI wellness coach<br>🌸 A plan built around your goals")
        + email_send._button(cta, "Start My Free Trial")
        + "<p style='color:#8a7886'>Whenever you're ready 💗<br>The Caloria Club Team</p>"
        + f"<p style='color:#b7a9b2;font-size:11px;margin-top:22px'>You're receiving this "
          f"because you asked to hear from Caloria Club. "
          f"<a href='{unsub}' style='color:#b7a9b2'>Unsubscribe</a>.</p>"
    )
    html = email_send._shell("Your glow-up is waiting for you ♡", body)
    text = ("Your personalized Caloria Club plan is ready — and your 3 days are free.\n"
            f"Start: {cta}\nUnsubscribe: {unsub}")
    return html, text


def _email_b(name: str, user_ref: str):
    cta = f"{_SITE_BASE}/?checkout=resume"
    unsub = unsub_url(user_ref)
    body = (
        f"<p>Hi {name or 'there'},</p>"
        "<p>You're so close! Your Caloria Club membership is one tap from being active, "
        "and your <b>3-day free trial</b> is still waiting for you — no charge today.</p>"
        + email_send._button(cta, "Complete Your Checkout")
        + "<p style='color:#8a7886'>See you inside 💗<br>The Caloria Club Team</p>"
        + f"<p style='color:#b7a9b2;font-size:11px;margin-top:22px'>"
          f"<a href='{unsub}' style='color:#b7a9b2'>Unsubscribe from these reminders</a>.</p>"
    )
    html = email_send._shell("You're so close to your glow-up!", body)
    text = ("Your Caloria Club membership is one step away — your 3-day free trial is waiting.\n"
            f"Finish: {cta}\nUnsubscribe: {unsub}")
    return html, text


def _email_c(name: str, user_ref: str):
    cta = f"{_SITE_BASE}/?verify=1"
    unsub = unsub_url(user_ref)
    body = (
        f"<p>Hi {name or 'there'},</p>"
        "<p>Your Caloria Club account is almost ready — just confirm your email to "
        "finish setting it up and unlock your personalized plan.</p>"
        + email_send._button(cta, "Complete My Registration")
        + "<p style='color:#8a7886'>Can't wait to have you 💗<br>The Caloria Club Team</p>"
        + f"<p style='color:#b7a9b2;font-size:11px;margin-top:22px'>"
          f"<a href='{unsub}' style='color:#b7a9b2'>Unsubscribe from these reminders</a>.</p>"
    )
    html = email_send._shell("Your Caloria Club account is waiting ♡", body)
    text = ("Your Caloria Club account is almost ready — confirm your email to finish.\n"
            f"Continue: {cta}\nUnsubscribe: {unsub}")
    return html, text


_SUBJECTS = {
    "email_a": "Your glow-up is waiting for you ♡",
    "email_b": "You're so close to your glow-up!",
    "email_c": "Your Caloria Club account is waiting ♡",
}


def _deliver(ref: str, email_type: str, to: str, html: str, text: str) -> bool:
    """Claim → send → record. Returns True if an email was actually delivered."""
    if _already_sent(ref, email_type):
        return False
    if not _test_ok(to):
        return False
    subject = _SUBJECTS[email_type]
    headers = {"List-Unsubscribe": f"<{unsub_url(ref)}>",
               "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    try:
        email_send._send(to, subject, html, text, headers=headers,
                         idempotency_key=f"recovery:{email_type}:{ref}",
                         category="recovery")
        _record(ref, email_type, "sent")
        print(f"[caloria][recovery] {email_type} sent ref={ref}")
        return True
    except Exception as e:  # noqa: BLE001 — one failure must not stop the batch
        _record(ref, email_type, "failed")
        print(f"[caloria][recovery] {email_type} FAILED ref={ref}: {e}")
        return False


# --------------------------------------------------------------------------- #
# candidate selection (live re-check happens again inside process_due)
# --------------------------------------------------------------------------- #
def _candidates_a():
    """Leads: email captured, consented, still NO account, past the A delay."""
    with db.cursor() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, email FROM leads "
            "WHERE registered_user_id IS NULL AND marketing_consent = 1 "
            "AND COALESCE(unsubscribed,0) = 0 AND email IS NOT NULL AND email <> '' "
            "AND created_at <= ? AND created_at >= ?",
            (_ago(config.RECOVERY_EMAIL_A_MINUTES),
             _ago(config.RECOVERY_MAX_AGE_HOURS * 60)),
        ).fetchall()]


def _candidates_b():
    """Users with an open checkout attempt past the B delay."""
    with db.cursor() as c:
        return [dict(r) for r in c.execute(
            "SELECT ca.user_id AS id, u.email, u.name, u.plan, u.subscription_status, "
            "COALESCE(u.broadcast_unsubscribed,0) AS unsub, COALESCE(u.active,1) AS active "
            "FROM checkout_attempts ca JOIN users u ON u.id = ca.user_id "
            "WHERE ca.completed_at IS NULL AND ca.started_at <= ? AND ca.started_at >= ?",
            (_ago(config.RECOVERY_EMAIL_B_MINUTES),
             _ago(config.RECOVERY_MAX_AGE_HOURS * 60)),
        ).fetchall()]


def _candidates_c():
    """Users registered but NOT verified, who never started checkout, past the C delay."""
    with db.cursor() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, email, name, plan, subscription_status, "
            "COALESCE(broadcast_unsubscribed,0) AS unsub, COALESCE(active,1) AS active "
            "FROM users u WHERE COALESCE(email_verified,0) = 0 "
            "AND COALESCE(active,1) = 1 "
            "AND created_at <= ? AND created_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM checkout_attempts ca WHERE ca.user_id = u.id)",
            (_ago(config.RECOVERY_EMAIL_C_MINUTES),
             _ago(config.RECOVERY_MAX_AGE_HOURS * 60)),
        ).fetchall()]


def _user_row(user_id: int):
    with db.cursor() as c:
        return c.execute(
            "SELECT id, email, name, plan, subscription_status, email_verified, "
            "COALESCE(broadcast_unsubscribed,0) AS unsub, COALESCE(active,1) AS active "
            "FROM users WHERE id = ?", (int(user_id),),
        ).fetchone()


def _account_exists(email: str) -> bool:
    with db.cursor() as c:
        return c.execute("SELECT 1 FROM users WHERE email = ?",
                         ((email or "").strip().lower(),)).fetchone() is not None


# --------------------------------------------------------------------------- #
# scheduler tick
# --------------------------------------------------------------------------- #
def process_due(limit: int | None = None) -> dict:
    """One pass. Re-checks every suppression rule against LIVE state immediately
    before sending, so a step completed since selection cancels the email."""
    cap = limit if limit is not None else config.RECOVERY_MAX_PER_TICK
    counts = {"email_a": 0, "email_b": 0, "email_c": 0}
    sent_total = 0

    # ---- Email A — marketing, consent-gated, lead has no account ----
    for lead in _candidates_a():
        if sent_total >= cap:
            break
        ref, email = lead["id"], lead["email"]
        if _already_sent(ref, "email_a"):
            continue
        # Live re-check: must still be consented, not unsubscribed, no account yet,
        # and the email must not belong to an existing / converted customer.
        fresh = leads.get(ref)
        with db.cursor() as c:
            lr = c.execute(
                "SELECT marketing_consent, COALESCE(unsubscribed,0) AS u, registered_user_id "
                "FROM leads WHERE id = ?", (ref,)).fetchone()
        if not lr or not lr["marketing_consent"] or lr["u"] or lr["registered_user_id"]:
            continue
        if _account_exists(email):
            continue
        name = ((fresh or {}).get("profile") or {}).get("name", "") if fresh else ""
        html, text = _email_a(name, ref)
        if _deliver(ref, "email_a", email, html, text):
            counts["email_a"] += 1; sent_total += 1

    # ---- Email B — abandoned checkout (registered user, never converted) ----
    for cand in _candidates_b():
        if sent_total >= cap:
            break
        ref = f"user:{cand['id']}"
        if _already_sent(ref, "email_b"):
            continue
        u = _user_row(cand["id"])
        if not u or not u["active"] or u["unsub"] or _converted(u):
            continue
        html, text = _email_b(u["name"], ref)
        if _deliver(ref, "email_b", u["email"], html, text):
            counts["email_b"] += 1; sent_total += 1

    # ---- Email C — incomplete registration (unverified, no checkout) ----
    for cand in _candidates_c():
        if sent_total >= cap:
            break
        ref = f"user:{cand['id']}"
        if _already_sent(ref, "email_c"):
            continue
        u = _user_row(cand["id"])
        # Must still be unverified, active, not opted-out, not converted. If they
        # have since started checkout, they belong to Email B — skip here.
        if not u or not u["active"] or u["unsub"] or u["email_verified"] or _converted(u):
            continue
        with db.cursor() as c:
            started = c.execute("SELECT 1 FROM checkout_attempts WHERE user_id = ?",
                                (cand["id"],)).fetchone()
        if started:
            continue
        html, text = _email_c(u["name"], ref)
        if _deliver(ref, "email_c", u["email"], html, text):
            counts["email_c"] += 1; sent_total += 1

    return counts


def run_scheduler(interval_seconds: int | None = None) -> None:
    """Daemon loop — only auto-sends when config.recovery_ready() is true."""
    interval = interval_seconds or config.RECOVERY_TICK_SECONDS
    while True:
        try:
            if config.recovery_ready():
                result = process_due()
                if any(result.values()):
                    print(f"[caloria][recovery] sent {result}")
        except Exception as e:  # noqa: BLE001
            print(f"[caloria][recovery] scheduler error: {e}")
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# admin: leads & conversion analytics
# --------------------------------------------------------------------------- #
def admin_stats() -> dict:
    with db.cursor() as c:
        total_leads = c.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
        consented = c.execute("SELECT COUNT(*) n FROM leads WHERE marketing_consent=1").fetchone()["n"]
        registered = c.execute("SELECT COUNT(*) n FROM leads WHERE registered_user_id IS NOT NULL").fetchone()["n"]
        verified = c.execute("SELECT COUNT(*) n FROM leads WHERE email_verified_at IS NOT NULL").fetchone()["n"]
        checkout_started = c.execute("SELECT COUNT(*) n FROM checkout_attempts").fetchone()["n"]
        checkout_done = c.execute("SELECT COUNT(*) n FROM checkout_attempts WHERE completed_at IS NOT NULL").fetchone()["n"]
        abandoned = c.execute("SELECT COUNT(*) n FROM checkout_attempts WHERE completed_at IS NULL").fetchone()["n"]
        active_trials = c.execute(
            "SELECT COUNT(*) n FROM users WHERE subscription_status='trialing'").fetchone()["n"]
        paid = c.execute(
            "SELECT COUNT(*) n FROM users WHERE plan<>'free' AND subscription_status IN ('active','past_due')"
        ).fetchone()["n"]
        by_type = {r["email_type"]: r["n"] for r in c.execute(
            "SELECT email_type, COUNT(*) n FROM recovery_email_sends WHERE status='sent' GROUP BY email_type"
        ).fetchall()}
    recovery_sent = sum(by_type.values())
    return {
        "total_leads": total_leads,
        "new_leads": max(0, total_leads - registered),
        "marketing_consented": consented,
        "registration_completed": registered,
        "email_verified": verified,
        "checkout_started": checkout_started,
        "checkout_completed": checkout_done,
        "abandoned_checkouts": abandoned,
        "active_trials": active_trials,
        "converted_paid": paid,
        "recovery_emails_sent": recovery_sent,
        "recovery_by_type": by_type,
        "recovery_conversion_rate": round(100.0 * paid / recovery_sent, 1) if recovery_sent else 0.0,
        "enabled": config.RECOVERY_EMAILS_ENABLED,
        "test_mode": bool(config.RECOVERY_TEST_EMAILS),
    }


def admin_leads(q: str = "", limit: int = 100) -> dict:
    q = (q or "").strip().lower()
    limit = max(1, min(500, int(limit or 100)))
    where, params = "", []
    if q:
        where = "WHERE email LIKE ?"
        params.append(f"%{q}%")
    with db.cursor() as c:
        rows = c.execute(
            "SELECT id, email, status, marketing_consent, registered_user_id, "
            "email_verified_at, unsubscribed, created_at, updated_at "
            f"FROM leads {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        sends = {}
        for r in c.execute("SELECT ref, email_type FROM recovery_email_sends WHERE status='sent'").fetchall():
            sends.setdefault(r["ref"], []).append(r["email_type"])
    out = []
    for r in rows:
        d = dict(r)
        d["recovery_sent"] = sends.get(r["id"], [])
        if r["registered_user_id"]:
            d["recovery_sent"] += sends.get(f"user:{r['registered_user_id']}", [])
        out.append(d)
    return {"stats": admin_stats(), "leads": out}
