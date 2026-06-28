"""The Caloria Journey engine — motivation, progress & retention.

Computes the entire emotional/retention layer from data the app already has
(the user's logged meals + their nutrition targets). No OpenAI, no API cost.

Powers, from one call:
  • Daily Motivation message (motivation.py, 365+ lines, daily rotation)
  • Glow Score (0-100) + sub-dimensions (Skin / Energy / Bloating / Recovery)
  • Transformation Journey stage (1-6)
  • Streaks (current, longest, this week/month consistency)
  • Achievements (with first-unlock persistence + "new badge" detection)
  • Personal milestones (progress made visible before weight loss is)
  • Weekly review (this-week summary + next-week focus)
  • Future You projection (behaviour-based, never medical)

Scaling note: everything is derived on read from existing tables, so there's no
write-amplification per meal. The only persisted state is the `achievements`
table (one row per user per badge). At larger scale the per-user compute is a
single indexed query over `meals`; results can be cached per (user, day) and the
daily message + weekly review are pure functions ideal for a CDN/edge cache.
"""
from __future__ import annotations

import datetime
import json

import db
import community
import insights
import motivation
import workout

# ---- Transformation Journey stages (by distinct days tracked) ----
STAGES = [
    {"n": 1, "name": "Starting",          "min_days": 0,  "blurb": "Every transformation begins with a single logged day."},
    {"n": 2, "name": "Building Momentum", "min_days": 3,  "blurb": "You're stacking days. Momentum is forming."},
    {"n": 3, "name": "Creating Discipline","min_days": 7, "blurb": "Showing up is becoming who you are."},
    {"n": 4, "name": "Lifestyle Upgrade", "min_days": 14, "blurb": "This isn't a phase anymore — it's your lifestyle."},
    {"n": 5, "name": "Transformation",    "min_days": 30, "blurb": "The quiet work is becoming visible."},
    {"n": 6, "name": "Best Version Era",  "min_days": 60, "blurb": "You're living as the woman you decided to become."},
]

# ---- Streak milestone names ----
STREAK_TIERS = [
    {"days": 7,  "name": "7 Day Momentum"},
    {"days": 14, "name": "14 Day Builder"},
    {"days": 30, "name": "30 Day Discipline"},
    {"days": 90, "name": "90 Day Lifestyle Shift"},
]

ACHIEVEMENTS = [
    {"key": "first_meal",   "title": "First Meal Logged",   "emoji": "🍽️", "desc": "You showed up. The journey is officially underway."},
    {"key": "first_week",   "title": "First Week Complete", "emoji": "🌱", "desc": "Seven days of caring for yourself."},
    {"key": "streak_7",     "title": "7-Day Streak",        "emoji": "🔥", "desc": "A full week of unbroken momentum."},
    {"key": "streak_30",    "title": "30-Day Streak",       "emoji": "👑", "desc": "Thirty days. This is who you are now."},
    {"key": "meals_100",    "title": "100 Meals Analyzed",  "emoji": "💯", "desc": "One hundred conscious decisions logged."},
    {"key": "consistency",  "title": "Consistency Champion","emoji": "🏆", "desc": "You showed up 20+ days in a single month."},
    {"key": "glow_builder", "title": "Glow-Up Builder",     "emoji": "✨", "desc": "Your Glow Score crossed 70 — it shows."},
    {"key": "first_workout","title": "First Workout Done",  "emoji": "💪", "desc": "You completed your first workout. Strength starts here."},
    {"key": "workouts_5",   "title": "Movement Habit",      "emoji": "🏋️‍♀️", "desc": "Five workouts completed — training is becoming a habit."},
    {"key": "first_post",   "title": "First Community Post", "emoji": "💬", "desc": "You shared with the community for the first time."},
    {"key": "best_version",  "title": "Best Version Energy", "emoji": "💎", "desc": "You reached the Best Version Era."},
]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _level(score, hi, mid):
    return "High" if score >= hi else "Medium" if score >= mid else "Low"


def _load(user_id):
    with db.cursor() as c:
        rows = c.execute(
            "SELECT data_json, created_at FROM meals WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
        prof = c.execute(
            "SELECT targets_json FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    meals = []
    for r in rows:
        try:
            m = json.loads(r["data_json"]) or {}
        except (TypeError, ValueError):
            m = {}
        try:
            dt = datetime.datetime.strptime(str(r["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            try:
                dt = datetime.datetime.strptime(str(r["created_at"])[:10], "%Y-%m-%d")
            except (TypeError, ValueError):
                continue
        m["_day"] = dt.date()
        m["_dt"] = dt
        meals.append(m)
    targets = {}
    if prof and prof["targets_json"]:
        try:
            targets = json.loads(prof["targets_json"])
        except (TypeError, ValueError):
            targets = {}
    return meals, targets


def _streaks(day_set, freezes=frozenset()):
    """(current_streak, longest_streak). A 'freeze' bridges a missed day so the
    streak isn't broken — the frozen day doesn't add to the count (Duolingo-style).
    """
    if not day_set:
        return 0, 0
    one = datetime.timedelta(days=1)
    today = datetime.date.today()
    # ---- current streak: walk back from today (today not-yet-logged ≠ broken) ----
    d = today
    if d not in day_set and d not in freezes:
        d -= one
    cur = 0
    while True:
        if d in day_set:
            cur += 1; d -= one
        elif d in freezes:
            d -= one  # bridge, no increment
        else:
            break
    # ---- longest streak across history, bridging freezes ----
    longest = 0
    for day in sorted(day_set):
        prev = day - one
        if prev in day_set or prev in freezes:
            continue  # not a run start
        length, n = 0, day
        while True:
            if n in day_set:
                length += 1; n += one
            elif n in freezes:
                n += one
            else:
                break
        longest = max(longest, length)
    return cur, max(longest, cur)


def load_freezes(user_id):
    with db.cursor() as c:
        rows = c.execute("SELECT date FROM streak_freezes WHERE user_id = ?", (user_id,)).fetchall()
    out = set()
    for r in rows:
        try:
            out.add(datetime.date.fromisoformat(r["date"]))
        except (TypeError, ValueError):
            pass
    return out


def _glow_components(meals_list, targets, days_logged):
    """THE single Glow Score calculation. Returns (score 0-100, dimensions dict).

    `meals_list` is any list of meal dicts with macros; `days_logged` is the
    number of distinct logged days in that window (for the consistency term).
    Used everywhere Glow appears so the number is identical across the app.
    """
    if not meals_list:
        return 0, {"skin": "Low", "energy": "Low", "bloating": "High", "recovery": "Low"}
    n = len(meals_list)
    avg = lambda k: sum(float(m.get(k) or 0) for m in meals_list) / n
    protein, fiber, sugar, sodium = avg("protein"), avg("fiber"), avg("sugar"), avg("sodium")
    target_protein = float(targets.get("protein") or 100)

    protein_q = _clamp((protein / max(target_protein / 3, 1)), 0, 1)
    fiber_q = _clamp(fiber / 8.0, 0, 1)
    sugar_q = _clamp(1 - (sugar / 30.0), 0, 1)
    sodium_q = _clamp(1 - (sodium / 1500.0), 0, 1)
    consistency_q = _clamp(days_logged / 7.0, 0, 1)

    score = int(_clamp(round(100 * (
        0.26 * protein_q + 0.20 * fiber_q + 0.16 * sugar_q +
        0.12 * sodium_q + 0.26 * consistency_q
    )), 0, 100))
    dims = {
        "skin": _level(0.5 * fiber_q + 0.5 * sugar_q, 0.66, 0.4),
        "energy": _level(0.5 * consistency_q + 0.5 * protein_q, 0.66, 0.4),
        "bloating": _level(1 - (0.5 * sodium_q + 0.5 * sugar_q), 0.6, 0.34),  # risk: low good
        "recovery": _level(protein_q, 0.66, 0.4),
    }
    return score, dims


def glow_score_for(meals_list, targets, days_logged):
    """Public single-source Glow Score (int) for an arbitrary meal window."""
    return _glow_components(meals_list, targets, days_logged)[0]


def _glow(meals, targets):
    """Glow for the last 7 days (hub view): (score, dimensions, days_logged_week)."""
    week_ago = datetime.date.today() - datetime.timedelta(days=6)
    recent = [m for m in meals if m["_day"] >= week_ago]
    days_logged_week = len({m["_day"] for m in recent})
    score, dims = _glow_components(recent, targets, days_logged_week)
    return score, dims, days_logged_week


def _stage(days_tracked):
    stage = STAGES[0]
    for s in STAGES:
        if days_tracked >= s["min_days"]:
            stage = s
    nxt = next((s for s in STAGES if s["min_days"] > days_tracked), None)
    return stage, nxt


def _sync_achievements(user_id, earned_keys):
    """Persist newly-earned badges; return (all_with_state, newly_unlocked_keys)."""
    with db.cursor() as c:
        have = {r["key"]: r["unlocked_at"] for r in c.execute(
            "SELECT key, unlocked_at FROM achievements WHERE user_id = ?", (user_id,)
        ).fetchall()}
        new = []
        for k in earned_keys:
            if k not in have:
                c.execute("INSERT OR IGNORE INTO achievements (user_id, key) VALUES (?,?)", (user_id, k))
                new.append(k)
        if new:
            have = {r["key"]: r["unlocked_at"] for r in c.execute(
                "SELECT key, unlocked_at FROM achievements WHERE user_id = ?", (user_id,)
            ).fetchall()}
    cards = []
    for a in ACHIEVEMENTS:
        unlocked = a["key"] in earned_keys
        cards.append({**a, "unlocked": unlocked, "unlocked_at": have.get(a["key"])})
    return cards, new


def _active_days(user_id, meal_days):
    """A day 'counts' if the user did ANY meaningful action: logged a meal,
    completed the daily check-in, or completed a workout. This is what streaks
    and consistency are built on — aligned with how the product is actually used.
    """
    days = set(meal_days)
    with db.cursor() as c:
        for r in c.execute("SELECT date FROM daily_rituals WHERE user_id=? AND energy IS NOT NULL", (user_id,)):
            try: days.add(datetime.date.fromisoformat(r["date"]))
            except (TypeError, ValueError): pass
    for d in workout.completion_days(user_id):  # ensures the table exists
        try: days.add(datetime.date.fromisoformat(d))
        except (TypeError, ValueError): pass
    return days


def compute(user_id: int, *, persist: bool = True) -> dict:
    meals, targets = _load(user_id)
    meal_days = {m["_day"] for m in meals}
    # streaks/consistency use ALL meaningful activity (meal · check-in · workout).
    day_set = _active_days(user_id, meal_days)
    today = datetime.date.today()
    week_ago = today - datetime.timedelta(days=6)
    prev_week_start = today - datetime.timedelta(days=13)
    month_ago = today - datetime.timedelta(days=29)

    meals_logged = len(meals)
    days_tracked = len(day_set)
    days_week = len({d for d in day_set if d >= week_ago})
    days_prev_week = len({d for d in day_set if prev_week_start <= d < week_ago})
    days_month = len({d for d in day_set if d >= month_ago})
    meals_week = len([m for m in meals if m["_day"] >= week_ago])
    workouts_total = workout.completion_count(user_id)
    posts_total = community.post_count(user_id)
    cur_streak, longest_streak = _streaks(day_set, load_freezes(user_id))
    glow, dims, _ = _glow(meals, targets)
    stage, next_stage = _stage(days_tracked)

    # consistency improvement (this week vs last)
    if days_prev_week > 0:
        improvement = round((days_week - days_prev_week) / days_prev_week * 100)
    else:
        improvement = 100 if days_week > 0 else 0

    # ---- achievements ----
    earned = []
    if meals_logged >= 1: earned.append("first_meal")
    if days_tracked >= 7: earned.append("first_week")
    if longest_streak >= 7: earned.append("streak_7")
    if longest_streak >= 30: earned.append("streak_30")
    if meals_logged >= 100: earned.append("meals_100")
    if days_month >= 20: earned.append("consistency")
    if glow >= 70: earned.append("glow_builder")
    if workouts_total >= 1: earned.append("first_workout")
    if workouts_total >= 5: earned.append("workouts_5")
    if posts_total >= 1: earned.append("first_post")
    if stage["n"] >= 6: earned.append("best_version")
    if persist:
        achievement_cards, new_badges = _sync_achievements(user_id, earned)
    else:
        achievement_cards = [{**a, "unlocked": a["key"] in earned, "unlocked_at": None} for a in ACHIEVEMENTS]
        new_badges = []

    # ---- streak tier ----
    streak_tier = None
    for t in STREAK_TIERS:
        if cur_streak >= t["days"]:
            streak_tier = t["name"]

    # ---- milestones (make progress visible) ----
    milestones = []
    if meals_week:
        milestones.append(f"You've made {meals_week} healthy decisions this week.")
    if days_month:
        milestones.append(f"You've shown up for yourself {days_month} days this month.")
    if days_prev_week > 0 and improvement >= 100:
        milestones.append("You've more than doubled your consistency vs last week.")
    elif improvement > 0 and days_prev_week > 0:
        milestones.append(f"You've improved your consistency by {improvement}% over last week.")
    if cur_streak >= 2:
        milestones.append(f"You're on a {cur_streak}-day streak. Momentum is real.")
    if meals_logged:
        milestones.append(f"{meals_logged} meals logged on your journey so far.")
    if not milestones:
        milestones.append("Log your first meal to start making your progress visible.")

    # ---- weekly review ----
    wins = []
    if days_week: wins.append(f"Logged meals on {days_week} day{'s' if days_week != 1 else ''}")
    if days_week >= days_prev_week and days_prev_week > 0: wins.append("Held or improved your consistency")
    if glow >= 60: wins.append(f"Strong Glow Score ({glow})")
    if cur_streak >= 3: wins.append(f"Kept a {cur_streak}-day streak alive")
    if not wins: wins.append("Showed up to start — that counts")

    focus = []
    if dims["recovery"] != "High": focus.append("Add a protein source to one more meal")
    if dims["skin"] != "High": focus.append("Lean into fibre — veg, berries, whole grains")
    if dims["bloating"] != "Low": focus.append("Go easy on salt and added sugar")
    if days_week < 5: focus.append("Aim to log one more day than last week")
    focus.append("Keep the streak alive")
    focus = focus[:3]

    # ---- Future You (behaviour-based, never medical) ----
    if improvement > 0 and days_prev_week > 0:
        future = ("Your consistency is stronger than last week. Small improvements like "
                  "this compound into a completely different you over the next 90 days.")
    elif cur_streak >= 7:
        future = ("You're behaving far more like your future self than your past self. "
                  "Keep this rhythm and the next season will feel transformative.")
    elif days_tracked >= 1:
        future = ("If you hold these habits, the next 90 days could completely change how "
                  "you feel in your body and how you see yourself.")
    else:
        future = ("The woman you're dreaming about is built by today's decisions. "
                  "Log your first day and start writing her story.")

    # ---- behavior-change insight layer ----
    patterns = insights.patterns(meals)
    glow_up_insights = insights.glow_insights(meals, targets, days_week)
    identity = insights.identity_line(cur_streak, days_tracked, improvement)

    # ---- daily motivation (offset per user so it's not identical for everyone) ----
    msg = motivation.today_message(offset=user_id % len(motivation.MESSAGES))

    return {
        "daily_message": msg["message"],
        "date": msg["date"],
        "motivation_library_size": msg["total_messages"],
        "glow_score": glow,
        "glow_dimensions": {
            "skin_support": dims["skin"],
            "energy_support": dims["energy"],
            "bloating_risk": dims["bloating"],
            "recovery_support": dims["recovery"],
        },
        "stage": {"number": stage["n"], "name": stage["name"], "blurb": stage["blurb"]},
        "next_stage": ({"name": next_stage["name"], "days_to_go": next_stage["min_days"] - days_tracked}
                       if next_stage else None),
        "stages": [{"number": s["n"], "name": s["name"], "reached": days_tracked >= s["min_days"]} for s in STAGES],
        "streak": {"current": cur_streak, "longest": longest_streak, "tier": streak_tier},
        "consistency": {
            "days_this_week": days_week, "days_last_week": days_prev_week,
            "days_this_month": days_month, "improvement_pct": improvement,
        },
        "totals": {"meals_logged": meals_logged, "days_tracked": days_tracked,
                   "workouts_completed": workouts_total, "posts_created": posts_total},
        "targets": targets,
        "achievements": achievement_cards,
        "new_achievements": [a for a in achievement_cards if a["key"] in new_badges],
        "milestones": milestones,
        "weekly_review": {"wins": wins, "next_week_focus": focus},
        "future_you": future,
        "identity_line": identity,
        "patterns": patterns,
        "glow_up_insights": glow_up_insights,
    }
