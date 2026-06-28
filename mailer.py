"""Intelligent retention emails (Resend) — personalised, never spammy.

Four kinds, all built from the user's real activity:
  • Sunday Reset   — weekly summary + Future You reflection (Sundays)
  • Midweek        — one mid-week encouragement tied to this week's progress
  • Re-engagement  — gentle nudges at 3 / 5 / 7 days inactive (never shaming)
  • Milestone      — when a streak / Glow / consistency milestone is reached

Safety: the background scheduler only runs when EMAIL_RETENTION_ENABLED=true.
Every send is de-duplicated via the email_log table (one per user/type/period),
so nobody is ever emailed twice for the same thing. Admins can preview/test a
single email regardless of the flag.
"""
from __future__ import annotations

import datetime

import config
import db
import email_send
import journey
import ritual


def _period_week():
    y, w, _ = datetime.date.today().isocalendar()
    return f"{y}-W{w:02d}"


def _logged(user_id, kind, period) -> bool:
    with db.cursor() as c:
        return c.execute(
            "SELECT 1 FROM email_log WHERE user_id=? AND type=? AND period=?",
            (user_id, kind, period),
        ).fetchone() is not None


def _mark(user_id, kind, period):
    with db.cursor() as c:
        c.execute("INSERT OR IGNORE INTO email_log (user_id, type, period) VALUES (?,?,?)",
                  (user_id, kind, period))


def _last_activity(user_id):
    with db.cursor() as c:
        m = c.execute("SELECT MAX(substr(created_at,1,10)) d FROM meals WHERE user_id=?", (user_id,)).fetchone()["d"]
        r = c.execute("SELECT MAX(date) d FROM daily_rituals WHERE user_id=?", (user_id,)).fetchone()["d"]
    days = [d for d in (m, r) if d]
    return max(days) if days else None


def _emailable_users():
    with db.cursor() as c:
        return c.execute(
            "SELECT id, email, name FROM users WHERE email_verified=1 AND active=1"
        ).fetchall()


# ---------------- content builders ----------------
def build_sunday(user_id):
    s = ritual.sunday_reset(user_id)
    return {
        "stats": [
            ("Days this week", s["consistency"]["days_this_week"]),
            ("Current streak", f"{s['streak']['current']} 🔥"),
            ("Glow Score", s["glow_score"]),
            ("Mood", s["mood_trend"]),
            ("Energy", s["energy_trend"]),
        ],
        "wins": s["wins"],
        "future_you": s["future_you_reflection"],
        "focus": ", ".join(s["next_week_focus"][:2]),
    }


def build_midweek(user_id):
    j = journey.compute(user_id, persist=False)
    days = j["consistency"]["days_this_week"]
    streak = j["streak"]["current"]
    if streak >= 3:
        return f"You're on a {streak}-day streak and {days} days in this week — beautiful momentum. Keep showing up; Future You can feel it."
    if days >= 2:
        return f"You've shown up {days} days this week already. One more check-in keeps the rhythm going strong."
    return "A new day is a fresh chance to show up for yourself. A 10-second check-in is all it takes to get the week moving."


def build_reengage(user_id, days_inactive):
    if days_inactive >= 7:
        return "It's been about a week — and your journey hasn't gone anywhere. Future You is still here, still cheering, ready whenever you are. One small check-in begins again."
    if days_inactive >= 5:
        return "A few days away changes nothing about who you're becoming. Come back when you're ready — Future You would love to see you today."
    return "We noticed you've been away for a couple of days. No guilt, ever — just a gentle nudge that a 10-second check-in keeps your momentum alive."


def milestone_for(user_id):
    """Return (key, headline, body) for a freshly-reached milestone, or None."""
    j = journey.compute(user_id, persist=False)
    streak = j["streak"]["current"]
    glow = j["glow_score"]
    days_month = j["consistency"]["days_this_month"]
    if streak >= 90: return ("streak_90", "90-Day Lifestyle Shift", "Ninety days. This isn't a phase anymore — it's who you are.")
    if streak >= 30: return ("streak_30", "30-Day Discipline", "Thirty days of showing up. Future You is in awe of you.")
    if streak >= 14: return ("streak_14", "14 Day Builder", "Two weeks strong — you're building habits that last.")
    if streak >= 7:  return ("streak_7", "7 Day Momentum", "A full week of momentum. This is how transformations begin.")
    if glow >= 80:   return ("glow_80", "Glowing", "Your Glow Score crossed 80 — your choices are showing.")
    if days_month >= 20: return ("consistency_20", "Consistency Champion", "20+ days of caring for yourself this month. Incredible.")
    return None


# ---------------- send one (used by scheduler + admin test) ----------------
def send_one(user_id, kind, *, force=False) -> dict:
    with db.cursor() as c:
        u = c.execute("SELECT email, name FROM users WHERE id=?", (user_id,)).fetchone()
    if not u or not u["email"]:
        return {"sent": False, "reason": "no email"}
    to, name = u["email"], u["name"] or ""
    week = _period_week()

    if kind == "sunday":
        if not force and _logged(user_id, kind, week):
            return {"sent": False, "reason": "already sent this week"}
        email_send.send_sunday_reset(to, name, build_sunday(user_id))
        _mark(user_id, kind, week)
    elif kind == "midweek":
        if not force and _logged(user_id, kind, week):
            return {"sent": False, "reason": "already sent this week"}
        email_send.send_midweek(to, name, build_midweek(user_id))
        _mark(user_id, kind, week)
    elif kind == "reengage":
        last = _last_activity(user_id)
        di = (datetime.date.today() - datetime.date.fromisoformat(last)).days if last else 99
        thresh = 7 if di >= 7 else 5 if di >= 5 else 3 if di >= 3 else 0
        if not thresh and not force:
            return {"sent": False, "reason": "user is active"}
        period = f"{last or 'never'}+{thresh or 3}"
        if not force and _logged(user_id, kind, period):
            return {"sent": False, "reason": "already sent for this gap"}
        email_send.send_reengagement(to, name, build_reengage(user_id, thresh or 3))
        _mark(user_id, kind, period)
    elif kind == "milestone":
        ms = milestone_for(user_id)
        if not ms and not force:
            return {"sent": False, "reason": "no milestone"}
        key, headline, body = ms or ("test", "✨ Test Milestone", "A sample milestone email.")
        if not force and _logged(user_id, kind, key):
            return {"sent": False, "reason": "milestone already emailed"}
        email_send.send_milestone(to, name, headline, body)
        _mark(user_id, kind, key)
    else:
        return {"sent": False, "reason": "unknown type"}
    return {"sent": True, "type": kind, "to": to}


# ---------------- scheduler tick (auto, flag-gated) ----------------
def process_all() -> dict:
    today = datetime.date.today()
    weekday = today.weekday()  # Mon=0 .. Sun=6
    counts = {"sunday": 0, "midweek": 0, "reengage": 0, "milestone": 0}
    for u in _emailable_users():
        uid = u["id"]
        try:
            last = _last_activity(uid)
            di = (today - datetime.date.fromisoformat(last)).days if last else None
            # milestone (any day)
            if send_one(uid, "milestone")["sent"]:
                counts["milestone"] += 1
            # Sunday Reset (only Sundays, only for users active in last 10 days)
            if weekday == 6 and di is not None and di <= 10 and send_one(uid, "sunday")["sent"]:
                counts["sunday"] += 1
            # Midweek (Wednesday, active this week)
            if weekday == 2 and di is not None and di <= 7 and send_one(uid, "midweek")["sent"]:
                counts["midweek"] += 1
            # Re-engagement (3/5/7 days inactive; stop after 7 so we never nag)
            if di is not None and 3 <= di <= 7 and send_one(uid, "reengage")["sent"]:
                counts["reengage"] += 1
        except Exception as e:  # noqa: BLE001 — one user's failure must not stop the run
            print(f"[caloria][mailer] failed for user {uid}: {e}")
    return counts


def run_scheduler(interval_seconds=3600):
    """Daemon loop — only auto-sends when EMAIL_RETENTION_ENABLED is true."""
    import time
    while True:
        try:
            if config.EMAIL_RETENTION_ENABLED and config.email_ready():
                result = process_all()
                if any(result.values()):
                    print(f"[caloria][mailer] sent {result}")
        except Exception as e:  # noqa: BLE001
            print(f"[caloria][mailer] scheduler error: {e}")
        time.sleep(interval_seconds)
