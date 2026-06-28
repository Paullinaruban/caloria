"""The Daily Ritual system — the emotional center & retention engine of Caloria.

A < 15-second daily loop (morning check-in + evening reflection) that gives
users a reason to open the app every day. All local, no OpenAI:

  • Morning check-in (energy / mood / sleep / hydration)
  • Evening reflection (how today felt — reflection, never guilt)
  • Daily Coach Message (personalised from the check-in)
  • Best Version Meter (0-100 identity score)
  • First Week Quest (7-day guided onboarding)
  • Sunday Reset (weekly luxury review)
  • Streak Protection (one free freeze per ISO week)

Built on the existing meals + new daily_rituals/streak_freezes tables; reuses
the journey engine for streak/consistency so everything stays consistent.
"""
from __future__ import annotations

import datetime

import db
import journey
import workout

# Allowed check-in values (validated on write).
ENERGY = ["Low", "Medium", "High"]
MOOD = ["Struggling", "Neutral", "Good", "Great"]
SLEEP = ["Poor", "Okay", "Good"]
HYDRATION = ["Behind", "Average", "On Track"]
REFLECTIONS = ["Proud of myself", "Stayed consistent", "Could have done better",
               "Learned something", "Tomorrow will be better"]

_RANK = {
    "energy": {"Low": 0, "Medium": 1, "High": 2},
    "mood": {"Struggling": 0, "Neutral": 1, "Good": 2, "Great": 3},
    "sleep": {"Poor": 0, "Okay": 1, "Good": 2},
    "hydration": {"Behind": 0, "Average": 1, "On Track": 2},
}


def _today():
    return datetime.date.today().isoformat()


def _iso_week(d: datetime.date | None = None) -> str:
    d = d or datetime.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# ---------------- check-in & reflection ----------------
def save_checkin(user_id: int, energy, mood, sleep, hydration) -> dict:
    if energy not in ENERGY or mood not in MOOD or sleep not in SLEEP or hydration not in HYDRATION:
        raise ValueError("Invalid check-in values.")
    today = _today()
    with db.cursor() as c:
        c.execute("INSERT OR IGNORE INTO daily_rituals (user_id, date) VALUES (?,?)", (user_id, today))
        c.execute(
            "UPDATE daily_rituals SET energy=?, mood=?, sleep=?, hydration=? WHERE user_id=? AND date=?",
            (energy, mood, sleep, hydration, user_id, today),
        )
    return state(user_id)


def save_reflection(user_id: int, reflection) -> dict:
    if reflection not in REFLECTIONS:
        raise ValueError("Invalid reflection value.")
    today = _today()
    with db.cursor() as c:
        c.execute("INSERT OR IGNORE INTO daily_rituals (user_id, date) VALUES (?,?)", (user_id, today))
        c.execute("UPDATE daily_rituals SET reflection=? WHERE user_id=? AND date=?",
                  (reflection, user_id, today))
    return state(user_id)


def _today_row(user_id):
    with db.cursor() as c:
        return c.execute(
            "SELECT energy, mood, sleep, hydration, reflection FROM daily_rituals "
            "WHERE user_id=? AND date=?", (user_id, _today())
        ).fetchone()


# ---------------- daily coach message ----------------
def coach_message(energy, mood, sleep, hydration) -> str:
    e, m, s = _RANK["energy"].get(energy, 1), _RANK["mood"].get(mood, 1), _RANK["sleep"].get(sleep, 1)
    h = _RANK["hydration"].get(hydration, 1)
    # high-priority supportive responses
    if m == 0:
        return ("Some days are heavier than others — and you still showed up. Be gentle "
                "with yourself today. Future You isn't keeping score; consistency, not "
                "perfection, is the win.")
    if s >= 2 and e >= 2:
        return ("You slept well and your energy is high — today is a beautiful day to "
                "build momentum. Make one choice Future You will thank you for.")
    if e == 0 or s == 0:
        return ("Your energy feels lower today. That's okay — focus on consistency over "
                "intensity. One small choice still moves Future You forward.")
    if h == 0:
        return ("Let's start with water today 💧 — hydration is the quiet foundation of "
                "energy, focus and a good mood. Future You will feel it.")
    if m >= 2 and e >= 1:
        return ("You're feeling good and showing up — that's exactly how Future You is "
                "built. Keep the rhythm going.")
    return ("Your habits matter more than your motivation. Show up for yourself today, "
            "however that looks — Future You is watching, and proud.")


# ---------------- Best Version Meter ----------------
def best_version_meter(user_id: int) -> dict:
    """0-100 identity score from consistency + check-ins + logging + streak."""
    j = journey.compute(user_id, persist=False)
    days_week = j["consistency"]["days_this_week"]
    streak = j["streak"]["current"]
    meals = j["totals"]["meals_logged"]
    # check-ins in the last 7 days
    week_ago = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
    with db.cursor() as c:
        checkins = c.execute(
            "SELECT COUNT(*) n FROM daily_rituals WHERE user_id=? AND date>=? "
            "AND energy IS NOT NULL", (user_id, week_ago)
        ).fetchone()["n"]

    consistency_q = min(days_week / 7.0, 1)
    checkin_q = min(checkins / 7.0, 1)
    streak_q = min(streak / 14.0, 1)
    logging_q = min(meals / 21.0, 1)  # ~3/day for a week
    score = round(100 * (0.32 * consistency_q + 0.28 * checkin_q + 0.22 * streak_q + 0.18 * logging_q))
    score = max(0, min(100, score))

    if score >= 85:
        line = "Your actions increasingly match your goals. This is who you are now."
    elif score >= 70:
        line = "You are becoming the person you promised yourself you would be."
    elif score >= 45:
        line = "The gap between your goals and your habits is shrinking every day."
    elif score >= 20:
        line = "You're laying the foundation. Every check-in builds your best version."
    else:
        line = "Your best version starts with today's first small choice."
    return {"score": score, "message": line}


# ---------------- First Week Quest ----------------
QUEST_DAYS = [
    {"day": 1, "title": "Complete your first check-in", "key": "checkin"},
    {"day": 2, "title": "Log a meal", "key": "log_meal"},
    {"day": 3, "title": "Build momentum", "key": "momentum"},
    {"day": 4, "title": "Notice your patterns", "key": "patterns"},
    {"day": 5, "title": "Complete a workout from your plan", "key": "workout"},
    {"day": 6, "title": "Create your first community post", "key": "community_post"},
    {"day": 7, "title": "Protect your streak", "key": "protect"},
    {"day": 8, "title": "Complete your first transformation week", "key": "week"},
]


def quest(user_id: int, j: dict | None = None) -> dict:
    j = j or journey.compute(user_id, persist=False)
    meals = j["totals"]["meals_logged"]
    days_tracked = j["totals"]["days_tracked"]
    streak = j["streak"]["current"]
    with db.cursor() as c:
        checkin_days = c.execute(
            "SELECT COUNT(*) n FROM daily_rituals WHERE user_id=? AND energy IS NOT NULL",
            (user_id,)
        ).fetchone()["n"]
        froze = c.execute("SELECT COUNT(*) n FROM streak_freezes WHERE user_id=?",
                          (user_id,)).fetchone()["n"]
    active_days = max(days_tracked, checkin_days)
    done = {
        "checkin": checkin_days >= 1,
        "log_meal": meals >= 1,
        "momentum": active_days >= 2,
        "patterns": days_tracked >= 3 or len(j.get("patterns", [])) > 0,
        "workout": j["totals"].get("workouts_completed", 0) >= 1,
        "community_post": j["totals"].get("posts_created", 0) >= 1,
        "protect": streak >= 3 or froze >= 1,
        "week": active_days >= 7,
    }
    steps = [{**d, "done": done.get(d["key"], False)} for d in QUEST_DAYS]
    completed = sum(1 for s in steps if s["done"])
    return {
        "steps": steps,
        "completed": completed,
        "total": len(steps),
        "all_done": completed == len(steps),
        "active": active_days < 7 and not (completed == len(steps)),
    }


# ---------------- streak freeze ----------------
def freeze_status(user_id: int) -> dict:
    """One freeze per ISO week, counted by when the freeze was *used* (created)."""
    week = _iso_week()
    with db.cursor() as c:
        rows = c.execute("SELECT created_at FROM streak_freezes WHERE user_id=?",
                         (user_id,)).fetchall()
    used_this_week = False
    for r in rows:
        try:
            if _iso_week(datetime.date.fromisoformat(str(r["created_at"])[:10])) == week:
                used_this_week = True
                break
        except (TypeError, ValueError):
            continue
    return {"available": not used_this_week, "used_this_week": used_this_week}


def use_freeze(user_id: int) -> dict:
    """Protect yesterday (the day at risk). One free freeze per ISO week."""
    if not freeze_status(user_id)["available"]:
        raise ValueError("You've already used your streak freeze this week.")
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    with db.cursor() as c:
        c.execute("INSERT OR IGNORE INTO streak_freezes (user_id, date) VALUES (?,?)",
                  (user_id, yesterday))
    return state(user_id)


# ---------------- Sunday Reset ----------------
def _week_trends(user_id):
    """Mood/energy trend + check-in count, this week vs last (from daily_rituals)."""
    today = datetime.date.today()
    week_ago = today - datetime.timedelta(days=6)
    prev_start = today - datetime.timedelta(days=13)
    rank = {"Struggling": 0, "Neutral": 1, "Good": 2, "Great": 3}
    erank = {"Low": 0, "Medium": 1, "High": 2}
    with db.cursor() as c:
        rows = c.execute(
            "SELECT date, energy, mood FROM daily_rituals WHERE user_id=? AND energy IS NOT NULL",
            (user_id,)).fetchall()
    def _avg(s, e, col, tbl):
        vals = [tbl.get(r[col], 1) for r in rows
                if r[col] and s <= datetime.date.fromisoformat(r["date"]) <= e]
        return sum(vals) / len(vals) if vals else None

    def _word(now, prev):
        if now is None:
            return "Log a check-in to track this"
        if prev is None:
            return "Building your baseline"
        if now > prev + 0.3:
            return "Trending up ↑"
        if now < prev - 0.3:
            return "A little lower — be gentle with yourself"
        return "Steady"

    mood = _word(_avg(week_ago, today, "mood", rank), _avg(prev_start, week_ago - datetime.timedelta(days=1), "mood", rank))
    energy = _word(_avg(week_ago, today, "energy", erank), _avg(prev_start, week_ago - datetime.timedelta(days=1), "energy", erank))
    checkins = len([r for r in rows if week_ago <= datetime.date.fromisoformat(r["date"]) <= today])
    return mood, energy, checkins


def sunday_reset(user_id: int, j: dict | None = None) -> dict:
    j = j or journey.compute(user_id, persist=False)
    cons = j["consistency"]
    mood_trend, energy_trend, checkins_week = _week_trends(user_id)
    biggest = None
    if cons["improvement_pct"] > 0 and cons["days_last_week"] > 0:
        biggest = f"Consistency up {min(cons['improvement_pct'], 100)}% vs last week"
    elif j["streak"]["current"] >= 3:
        biggest = f"A {j['streak']['current']}-day streak you protected"
    elif j["glow_score"] >= 60:
        biggest = f"A strong Glow Score of {j['glow_score']}"

    # workouts completed this week (real signal)
    week_ago = datetime.date.today() - datetime.timedelta(days=6)
    workouts_week = len([d for d in workout.completion_days(user_id)
                         if datetime.date.fromisoformat(d) >= week_ago])

    # Future You reflection — personalised + rotated so it's not identical weekly.
    _, wk, _ = datetime.date.today().isocalendar()
    pick = lambda opts: opts[(wk + user_id) % len(opts)]
    if cons["days_this_week"] >= 5:
        fy = pick([
            "Future You is genuinely proud of this week — you showed up like the woman you're becoming.",
            "This is the week Future You will point back to. You didn't just intend — you did.",
            "Future You can feel this week. Consistency like this is exactly how she was built.",
        ])
    elif cons["days_this_week"] >= 2:
        fy = pick([
            "Future You sees the effort you made this week — it matters more than you know. Let's build on it.",
            "A few real days of showing up — Future You is quietly cheering. Next week, one more.",
            "You moved the needle this week. Future You is paying attention to the small wins.",
        ])
    else:
        fy = pick([
            "Future You isn't keeping score. A fresh week is a fresh start, and she's already cheering.",
            "However light this week was, Future You is still here — ready to begin again with you.",
            "One quiet week doesn't change your direction. Future You is patient, and she believes in you.",
        ])

    # craving insight: prefer an actual craving pattern, else a neutral line.
    craving = next((p for p in j.get("patterns", []) if "crav" in p.lower()), None)

    return {
        "is_sunday": datetime.date.today().weekday() == 6,
        "wins": j["weekly_review"]["wins"],
        "consistency": {"days_this_week": cons["days_this_week"], "days_this_month": cons["days_this_month"]},
        "streak": j["streak"],
        "checkins_this_week": checkins_week,
        "workouts_this_week": workouts_week,
        "mood_trend": mood_trend,
        "energy_trend": energy_trend,
        "craving_insight": craving or "Keep logging to reveal your craving patterns.",
        "glow_score": j["glow_score"],
        "biggest_improvement": biggest or "You showed up — that's the foundation of everything",
        "future_you_reflection": fy,
        "next_week_focus": j["weekly_review"]["next_week_focus"],
        "encouragement": "Whatever this week looked like, you're still here — and that's what transformation is made of.",
    }


# ---------------- aggregate state for the app ----------------
def state(user_id: int) -> dict:
    row = _today_row(user_id)
    morning_done = bool(row and row["energy"])
    evening_done = bool(row and row["reflection"])
    hour = datetime.datetime.now().hour
    j = journey.compute(user_id, persist=False)

    msg = (coach_message(row["energy"], row["mood"], row["sleep"], row["hydration"])
           if morning_done else None)

    return {
        "date": _today(),
        "morning_done": morning_done,
        "evening_done": evening_done,
        "checkin": ({"energy": row["energy"], "mood": row["mood"], "sleep": row["sleep"],
                     "hydration": row["hydration"]} if morning_done else None),
        "reflection": row["reflection"] if evening_done else None,
        "coach_message": msg,
        "show_morning": not morning_done,
        "show_evening": morning_done and not evening_done and hour >= 17,
        "best_version": best_version_meter(user_id),
        "quest": quest(user_id, j),
        "streak": j["streak"],
        "freeze": freeze_status(user_id),
        "sunday_reset": sunday_reset(user_id, j),
        "options": {"energy": ENERGY, "mood": MOOD, "sleep": SLEEP,
                    "hydration": HYDRATION, "reflection": REFLECTIONS},
    }
