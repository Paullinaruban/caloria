"""Personalized, behavior-based notification & Future You engine.

In-app contextual notifications (no push / browser) generated from real activity
so the app feels like it notices the user. No random quotes.

Quality controls (audit fixes):
  • Every notification has a stable `key`.
  • Per-key COOLDOWN so the same message can't repeat daily (evergreen items
    rest 3-4 days; time-sensitive nudges may recur daily).
  • Seen-state via notification_log → the badge dot only lights for genuinely
    NEW items, not permanently.
  • Glow uses the single source of truth (journey.glow_score_for) so the numbers
    match the Journey hub.
  • Achievement notifications actually fire now (one-time each, via seen-state).
  • Future You phrasing rotates to avoid repetition.
"""
from __future__ import annotations

import datetime
import json

import db
import insights
import journey
import workout

_CRAVING_CATS = {"chocolate", "candy", "dessert", "fast_food", "fried", "soda", "sugary_drink"}

# Cooldown in days per notification key (or key prefix). Time-sensitive nudges
# recur daily; evergreen celebrations rest so they don't feel repetitive.
_COOLDOWN = {
    "streak_risk": 1, "checkin_nudge": 1, "reengage": 1, "protect_streak": 2,
    "milestone_close": 1, "glow_up": 4, "consistency_up": 4, "showing_up_more": 4,
    "cravings_down": 5, "mood_up": 4, "proud_today": 3, "monthly": 5,
    "workout_week": 4, "workout_nudge": 3,
    # achievements: effectively once-ever (long cooldown; keyed per badge)
    "ach": 3650,
}

# Rotating Future You fragments to reduce repetition.
_FY_PROUD = [
    "Future You is proud of you.", "Future You noticed.", "Future You felt that.",
    "Future You is quietly cheering.",
]
_FY_WAIT = [
    "Future You is still here, ready when you are.",
    "Future You hasn't gone anywhere — and neither has your progress.",
    "Future You is waiting, no pressure, just warmth.",
]


def _cooldown_for(key):
    if key.startswith("ach_"):
        return _COOLDOWN["ach"]
    return _COOLDOWN.get(key, 3)


def _last_shown(user_id):
    with db.cursor() as c:
        rows = c.execute("SELECT key, last_shown FROM notification_log WHERE user_id=?",
                         (user_id,)).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = datetime.date.fromisoformat(str(r["last_shown"])[:10]) if r["last_shown"] else None
        except (TypeError, ValueError):
            out[r["key"]] = None
    return out


def mark_seen(user_id, keys):
    """Record that these notification keys were shown to the user just now."""
    today = datetime.date.today().isoformat()
    with db.cursor() as c:
        for k in keys:
            c.execute(
                "INSERT INTO notification_log (user_id, key, last_shown) VALUES (?,?,?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET last_shown=excluded.last_shown",
                (user_id, k, today),
            )


def _load_days(user_id):
    with db.cursor() as c:
        meals = c.execute("SELECT name, created_at FROM meals WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        rituals = c.execute("SELECT date, energy, mood FROM daily_rituals WHERE user_id=? AND energy IS NOT NULL", (user_id,)).fetchall()
    meal_days, craving_days = set(), []
    last_meal = None
    for r in meals:
        d = str(r["created_at"])[:10]
        meal_days.add(d)
        last_meal = max(last_meal, d) if last_meal else d
        if insights.detect_category(r["name"] or "") in _CRAVING_CATS:
            craving_days.append(d)
    checkin_days = {r["date"] for r in rituals}
    last_checkin = max(checkin_days) if checkin_days else None
    moods = [(r["date"], r["mood"], r["energy"]) for r in rituals]
    return meal_days, craving_days, checkin_days, moods, last_meal, last_checkin


def _count_between(dates, start, end):
    return len([d for d in dates if start <= datetime.date.fromisoformat(d) <= end])


def _window_glow(user_id, targets, week_start, week_end):
    """Glow for a date window using the SINGLE source of truth (journey)."""
    with db.cursor() as c:
        rows = c.execute("SELECT data_json, created_at FROM meals WHERE user_id=?", (user_id,)).fetchall()
    subset, days = [], set()
    for r in rows:
        d = datetime.date.fromisoformat(str(r["created_at"])[:10])
        if week_start <= d <= week_end:
            try:
                subset.append(json.loads(r["data_json"]) or {})
                days.add(d)
            except (TypeError, ValueError):
                continue
    if not subset:
        return None
    return journey.glow_score_for(subset, targets, len(days))


def generate(user_id: int, limit: int = 6):
    """Return (items, has_new). Read-only — does NOT mark anything seen."""
    j = journey.compute(user_id, persist=False)
    meal_days, craving_days, checkin_days, moods, last_meal, last_checkin = _load_days(user_id)
    targets = j.get("targets") or {}
    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=6)
    prev_start = today - datetime.timedelta(days=13)
    today_s = today.isoformat()

    streak = j["streak"]["current"]
    days_month = j["consistency"]["days_this_month"]
    checkins_week = _count_between(checkin_days, week_start, today)
    checkins_prev = _count_between(checkin_days, prev_start, week_start - datetime.timedelta(days=1))
    cravings_week = _count_between(craving_days, week_start, today)
    cravings_prev = _count_between(craving_days, prev_start, week_start - datetime.timedelta(days=1))
    checked_in_today = today_s in checkin_days
    logged_today = today_s in meal_days
    workouts_total = j["totals"].get("workouts_completed", 0)
    workout_days = workout.completion_days(user_id)
    workouts_week = _count_between(workout_days, week_start, today)
    has_plan = workouts_total >= 0 and _has_workout_plan(user_id)

    glow_now = _window_glow(user_id, targets, week_start, today)
    glow_prev = _window_glow(user_id, targets, prev_start, week_start - datetime.timedelta(days=1))

    rank = {"Struggling": 0, "Neutral": 1, "Good": 2, "Great": 3}
    def _moodavg(s, e):
        vals = [rank.get(v[1], 1) for v in moods if v[1] and s <= datetime.date.fromisoformat(v[0]) <= e]
        return sum(vals) / len(vals) if vals else None
    mood_now = _moodavg(week_start, today)
    mood_prev = _moodavg(prev_start, week_start - datetime.timedelta(days=1))

    acts = [d for d in (last_meal, last_checkin, (max(workout_days) if workout_days else None)) if d]
    days_inactive = (today - datetime.date.fromisoformat(max(acts))).days if acts else None

    rot = lambda opts: opts[(today.timetuple().tm_yday + user_id) % len(opts)]
    C = []  # candidate (priority, key, dict)
    def add(pri, key, icon, title, body, tone="info", action=None):
        C.append((pri, key, {"icon": icon, "title": title, "body": body, "tone": tone, "action": action}))

    # re-engagement
    if days_inactive is not None and days_inactive >= 3:
        if days_inactive >= 7:
            add(0, "reengage", "💗", "Future You is waiting", f"It's been a little while — and that's okay. {rot(_FY_WAIT)} One small check-in begins again.", "warm", "checkin")
        elif days_inactive >= 5:
            add(1, "reengage", "🌱", "Your journey is still here", f"{days_inactive} days away changes nothing. {rot(_FY_WAIT)}", "warm", "checkin")
        else:
            add(2, "reengage", "✨", "You haven't checked in recently", f"{rot(_FY_WAIT)} A 10-second check-in keeps your momentum alive.", "warm", "checkin")

    # streak at risk / protect (after the activity fix, a check-in OR workout also saves it)
    freeze_ok = isinstance(j.get("freeze"), dict) and j["freeze"].get("available")
    if streak >= 2 and not logged_today and not checked_in_today and today_s not in workout_days:
        add(1, "streak_risk", "🔥", "Your streak is at risk today", f"You're on a {streak}-day streak. A check-in, a meal, or a workout keeps it alive — Future You is rooting for you.", "urgent", "checkin")
    elif streak >= 3 and freeze_ok:
        add(5, "protect_streak", "❄️", "Today's a great day to protect your streak", "You have a streak freeze available this week — your momentum is safe whatever the day brings.", "info", "freeze")

    if not checked_in_today and (days_inactive is None or days_inactive < 3):
        add(4, "checkin_nudge", "☀️", "Start your day with intention", "A 10-second check-in tells Future You how to support you today.", "info", "checkin")

    # workouts (real signal only)
    if workouts_week >= 1:
        add(5, "workout_week", "💪", "You've trained this week", f"{workouts_week} workout{'s' if workouts_week != 1 else ''} completed this week. Strength is part of your story now.", "celebrate")
    elif has_plan and workouts_total == 0:
        add(6, "workout_nudge", "🏋️‍♀️", "Your workout plan is ready", "Complete one workout this week — Future You is excited to move with you.", "info", "workout")

    # celebratory / progress
    if checked_in_today:
        add(6, "proud_today", "💫", rot(_FY_PROUD), "You showed up for yourself today. That's exactly how your best version is built.", "celebrate")
    if checkins_week >= 3:
        add(5, "consistency_up", "📈", "Your consistency is improving", f"You completed {checkins_week} check-ins this week. {rot(_FY_PROUD)}", "celebrate")
    if checkins_prev and checkins_week > checkins_prev:
        add(4, "showing_up_more", "🌟", "You're showing up more", f"{checkins_week} check-ins this week vs {checkins_prev} last week. Momentum is building.", "celebrate")
    if cravings_prev and cravings_week < cravings_prev:
        add(5, "cravings_down", "🛡️", "You're leveling up your choices", "Fewer cravings logged than last week — your future self can feel the difference.", "celebrate")
    if glow_now is not None and glow_prev is not None and glow_now > glow_prev + 1:
        add(4, "glow_up", "✨", "Your Glow Score increased this week", f"Up from {glow_prev} to {glow_now}. Your recent choices are paying off.", "celebrate")
    if mood_now is not None and mood_prev is not None and mood_now > mood_prev + 0.3:
        add(6, "mood_up", "🌈", "Your mood is trending up", "This week is feeling brighter than last. Keep tending to yourself.", "celebrate")

    # achievement notifications — fire ONCE each (keyed per badge, long cooldown).
    for a in j.get("achievements", []):
        if a.get("unlocked"):
            add(2, f"ach_{a['key']}", a["emoji"], "Achievement unlocked", f"{a['title']} — {rot(_FY_PROUD)}", "celebrate")

    # milestone proximity
    if streak in (2, 6, 13, 29):
        nxt = {2: 3, 6: 7, 13: 14, 29: 30}[streak]
        add(3, "milestone_close", "🎯", "You're one day from a milestone", f"Show up tomorrow to reach a {nxt}-day streak.", "info", "checkin")

    if days_month >= 4:
        add(8, "monthly", "💖", "You're showing up for yourself", f"{days_month} days of caring for yourself this month. That's who you are now.", "info")

    # ---- apply cooldowns + seen-state ----
    shown = _last_shown(user_id)
    C.sort(key=lambda x: x[0])
    items, keys_out, seen_titles = [], [], set()
    has_new = False
    for pri, key, n in C:
        last = shown.get(key)
        if last is not None and (today - last).days < _cooldown_for(key):
            continue  # still cooling down
        if n["title"] in seen_titles:
            continue
        seen_titles.add(n["title"])
        n["_key"] = key
        items.append(n); keys_out.append(key)
        if last is None or last < today:
            has_new = True
        if len(items) >= limit:
            break
    return items, has_new, keys_out


def _has_workout_plan(user_id):
    try:
        with db.cursor() as c:
            return c.execute("SELECT 1 FROM workouts WHERE user_id=? LIMIT 1", (user_id,)).fetchone() is not None
    except Exception:  # noqa: BLE001 — table may not exist yet
        return False


def future_you_empty() -> dict:
    return {
        "icon": "✨",
        "title": "Future You is already here",
        "body": "Your transformation starts with today's first small choice. Complete your check-in and Future You will start noticing.",
        "tone": "warm",
        "action": "checkin",
        "_key": "empty",
    }
