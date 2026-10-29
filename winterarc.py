"""
winterarc.py — Caloria Club "Winter Arc": a 30-day seasonal wellness journey.

A thin, additive feature that sits ON TOP of the existing app. The server is the
single source of truth for availability, completion and rewards — the client is
never trusted to decide what's unlocked. Content lives here (edit text without
touching UI). Progress is one row per user in the `winter_arc` table.

Availability rule (consistent everywhere): day 1 is available the moment the user
starts; one further day unlocks per calendar day after the start date. Missed
days stay open (catch-up is allowed) and a user never loses progress or is
required to keep a streak.
"""
from __future__ import annotations

import datetime
import json

import config
import db

DAYS = config.WINTER_ARC_DAYS  # 30

# Reward milestones — unlocked by NUMBER of completed days (never a perfect
# streak). Missing a day never revokes an already-earned reward.
MILESTONES = [
    {"at": 1,  "id": "starter",     "title": "Winter Arc Starter", "emoji": "❄️"},
    {"at": 7,  "id": "routine",     "title": "Routine Girl",       "emoji": "🎀"},
    {"at": 15, "id": "halfway",     "title": "Halfway There",      "emoji": "⭐"},
    {"at": 21, "id": "consistency", "title": "Consistency Club",   "emoji": "🍪"},
    {"at": 30, "id": "complete",    "title": "Winter Arc Complete","emoji": "🎁"},
]

# Each challenge: day, week, title, description, type, action (what "complete"
# means), optional `feature` linking to an EXISTING app view (scan=Meal Scan AI,
# coach=AI Coach, workout=Workout, plan=Meal plan, journey=Your Journey). A null
# feature is a pure reflection the user can finish without leaving the screen.
CHALLENGES = [
    # WEEK 1 — THE RESET
    {"day": 1,  "week": 1, "title": "Your Winter Arc Begins", "sticker": "❄️",
     "desc": "Choose one sustainable wellness intention for the next 30 days — then tap Mark as Complete to officially begin your Winter Arc.",
     "type": "reflection", "action": "Begin my Winter Arc", "feature": None},
    {"day": 2,  "week": 1, "title": "Build Your Balanced Plate", "sticker": "🍽️",
     "desc": "Use Meal Scan AI to analyze a meal, then explore one way to make it more balanced and satisfying. (No specific score required — just a little more balance.)",
     "type": "log", "action": "Open Meal Scan", "feature": "scan"},
    {"day": 3,  "week": 1, "title": "The Protein Upgrade", "sticker": "🥚",
     "desc": "Log or analyze a meal and identify one practical way to add a protein source you genuinely enjoy.",
     "type": "log", "action": "Open Meal Scan", "feature": "scan"},
    {"day": 4,  "week": 1, "title": "Your Personal Movement Plan", "sticker": "🏃‍♀️",
     "desc": "Open your workout plan and choose a movement session that fits your schedule and energy today.",
     "type": "activity", "action": "Open Workout", "feature": "workout"},
    {"day": 5,  "week": 1, "title": "The Balanced Plate Challenge", "sticker": "🥗",
     "desc": "Use the food-analysis feature to review a meal's composition and spot one reasonable improvement.",
     "type": "log", "action": "Open Meal Scan", "feature": "scan"},
    {"day": 6,  "week": 1, "title": "Ask Your AI Coach", "sticker": "💬",
     "desc": "Ask your AI Coach a specific question about a wellness habit, and save or act on one useful suggestion.",
     "type": "coach", "action": "Open AI Coach", "feature": "coach"},
    {"day": 7,  "week": 1, "title": "Your First Weekly Review", "sticker": "🎀",
     "desc": "Reflect on your week and choose one habit that felt good and you'd like to continue.",
     "type": "reflection", "action": "Reflect & complete", "feature": None},
    # WEEK 2 — THE ROUTINE
    {"day": 8,  "week": 2, "title": "Breakfast Ritual", "sticker": "🥞",
     "desc": "Build a breakfast that leaves you satisfied and energized, then log it.",
     "type": "log", "action": "Log breakfast", "feature": "scan"},
    {"day": 9,  "week": 2, "title": "Make It Balanced", "sticker": "⚖️",
     "desc": "Practice combining protein, carbs, fats and fiber across your meals.",
     "type": "education", "action": "Reflect & complete", "feature": None},
    {"day": 10, "week": 2, "title": "Your Movement Moment", "sticker": "✨",
     "desc": "Choose a form of movement you genuinely enjoy.",
     "type": "activity", "action": "Open Workout", "feature": "workout"},
    {"day": 11, "week": 2, "title": "Food Awareness", "sticker": "📸",
     "desc": "Log a meal or try Meal Scan AI to see what's on your plate.",
     "type": "log", "action": "Open Meal Scan", "feature": "scan"},
    {"day": 12, "week": 2, "title": "The Recovery Edit", "sticker": "🛋️",
     "desc": "Make a little time for rest, relaxation or gentle mobility.",
     "type": "recovery", "action": "Reflect & complete", "feature": None},
    {"day": 13, "week": 2, "title": "Your Personal Routine", "sticker": "🌟",
     "desc": "Choose one small wellness habit that fits your real life.",
     "type": "reflection", "action": "Reflect & complete", "feature": None},
    {"day": 14, "week": 2, "title": "Two-Week Check-In", "sticker": "📈",
     "desc": "Reflect on your progress — no judging your body or your food choices.",
     "type": "reflection", "action": "See your Journey", "feature": "journey"},
    # WEEK 3 — THE GLOW
    {"day": 15, "week": 3, "title": "Halfway Celebration", "sticker": "⭐",
     "desc": "Celebrate the midpoint — streak or not, you showed up.",
     "type": "reflection", "action": "Celebrate & complete", "feature": None},
    {"day": 16, "week": 3, "title": "Discover Something New", "sticker": "🔍",
     "desc": "Explore a nutrition or wellness feature in Caloria you haven't tried.",
     "type": "explore", "action": "Reflect & complete", "feature": None},
    {"day": 17, "week": 3, "title": "Color Your Plate", "sticker": "🥗",
     "desc": "Include a fruit or vegetable you enjoy, and log the meal.",
     "type": "log", "action": "Log a meal", "feature": "scan"},
    {"day": 18, "week": 3, "title": "Your Energy Check-In", "sticker": "🔋",
     "desc": "Reflect on your energy and whether your routine feels sustainable.",
     "type": "reflection", "action": "Reflect & complete", "feature": None},
    {"day": 19, "week": 3, "title": "Train With Intention", "sticker": "💪",
     "desc": "Follow your workout if one's scheduled — otherwise rest or move gently.",
     "type": "activity", "action": "Open Workout", "feature": "workout"},
    {"day": 20, "week": 3, "title": "Your Favorite Meal", "sticker": "🍝",
     "desc": "Enjoy a satisfying meal and practice a positive relationship with food.",
     "type": "log", "action": "Log your meal", "feature": "scan"},
    {"day": 21, "week": 3, "title": "Reset Your Routine", "sticker": "🍪",
     "desc": "Choose one thing to keep, change, or simplify.",
     "type": "reflection", "action": "Reflect & complete", "feature": None},
    # WEEK 4 — THE UPGRADE
    {"day": 22, "week": 4, "title": "Your Strongest Habit", "sticker": "🏆",
     "desc": "Identify the habit that's been easiest to keep — and why.",
     "type": "reflection", "action": "Reflect & complete", "feature": None},
    {"day": 23, "week": 4, "title": "The Mindful Moment", "sticker": "🧘‍♀️",
     "desc": "Take a few minutes to pause, breathe, or simply reflect.",
     "type": "recovery", "action": "Reflect & complete", "feature": None},
    {"day": 24, "week": 4, "title": "Build Your Plate", "sticker": "🍲",
     "desc": "Create a balanced meal from foods you genuinely enjoy.",
     "type": "plan", "action": "Open Meal plan", "feature": "plan"},
    {"day": 25, "week": 4, "title": "Holiday Wellness", "sticker": "🎄",
     "desc": "Enjoy the holiday your way — no food guilt, no compensation, no mandatory workouts.",
     "type": "reflection", "action": "Reflect & complete", "feature": None},
    {"day": 26, "week": 4, "title": "Your Recovery Ritual", "sticker": "😴",
     "desc": "Prioritize rest, a sleep routine, or relaxing movement.",
     "type": "recovery", "action": "Reflect & complete", "feature": None},
    {"day": 27, "week": 4, "title": "Future You", "sticker": "🌱",
     "desc": "Choose one realistic habit you'd love to carry forward.",
     "type": "reflection", "action": "Reflect & complete", "feature": None},
    {"day": 28, "week": 4, "title": "Your Personal Progress", "sticker": "📖",
     "desc": "Reflect on what you've learned about your routine.",
     "type": "reflection", "action": "See your Journey", "feature": "journey"},
    {"day": 29, "week": 4, "title": "The Next Chapter", "sticker": "🔮",
     "desc": "Set one achievable intention for the coming month.",
     "type": "reflection", "action": "Reflect & complete", "feature": None},
    {"day": 30, "week": 4, "title": "Winter Arc Complete", "sticker": "🎁",
     "desc": "Celebrate your journey and unlock your final badge. You showed up for yourself.",
     "type": "reflection", "action": "Complete your Winter Arc", "feature": None},
]


class WinterArcError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message); self.message = message; self.code = code


def _today() -> datetime.date:
    return datetime.datetime.utcnow().date()


def _parse_date(s: str) -> datetime.date:
    # started_at is stored as an ISO date (or datetime) string, always UTC.
    return datetime.date.fromisoformat((s or "")[:10])


def current_day(started_at: str) -> int:
    """Highest day number currently AVAILABLE. Day 1 on start; +1 per calendar
    day; capped at DAYS. (Clock skew / a start 'in the future' still yields 1.)"""
    try:
        elapsed = (_today() - _parse_date(started_at)).days
    except Exception:
        elapsed = 0
    return max(1, min(DAYS, elapsed + 1))


def _rewards_for(completed_count: int) -> list:
    return [m["id"] for m in MILESTONES if completed_count >= m["at"]]


def content() -> dict:
    """Static, non-personalized content for the frontend (cacheable)."""
    return {
        "days": DAYS,
        "version": config.WINTER_ARC_VERSION,
        "challenges": CHALLENGES,
        "milestones": MILESTONES,
        "headline": "YOUR WINTER ARC STARTS NOW.",
        "tagline": "30 days of showing up for yourself. One little ritual at a time.",
    }


def _row(user_id: int):
    with db.cursor() as c:
        return c.execute("SELECT * FROM winter_arc WHERE user_id = ?", (user_id,)).fetchone()


def _state_from_row(row) -> dict:
    completed = sorted(set(json.loads(row["completed_days"] or "[]")))
    avail = current_day(row["started_at"])
    rewards = _rewards_for(len(completed))
    status = "completed" if len(completed) >= DAYS else "in_progress"
    return {
        "started": True,
        "start_date": row["started_at"][:10],
        "current_day": avail,                       # highest unlocked day
        "available_days": list(range(1, avail + 1)),
        "completed_days": completed,
        "completed_count": len(completed),
        "total": DAYS,
        "percent": round(100 * len(completed) / DAYS),
        "earned_rewards": rewards,
        "status": status,
        "version": row["version"] or config.WINTER_ARC_VERSION,
    }


def get_state(user_id: int) -> dict:
    row = _row(user_id)
    if not row:
        # NOT STARTED: the Winter Arc officially begins only when Day 1 is
        # completed. Day 1 is available immediately so the user can begin, but
        # the counter stays 0/30 and 0% until they actually complete it.
        return {"started": False, "start_date": None, "current_day": 1,
                "available_days": [1], "completed_days": [], "completed_count": 0,
                "total": DAYS, "percent": 0, "earned_rewards": [],
                "status": "not_started", "version": config.WINTER_ARC_VERSION}
    return _state_from_row(row)


def start(user_id: int) -> dict:
    """Kept for the Home CTA: a NO-OP that just returns current state. The Arc
    does NOT officially begin here — completing Day 1 is what records the start
    date (see complete_day). Never creates a progress row."""
    return get_state(user_id)


def complete_day(user_id: int, day: int) -> dict:
    """Mark a day complete. Validates ownership (caller passes the authed id),
    availability and range. Idempotent — completing an already-complete day is a
    no-op. Completing Day 1 for the first time OFFICIALLY STARTS the Winter Arc
    and records the authoritative server start date. Returns
    {state, newly_completed, new_rewards}."""
    try:
        day = int(day)
    except (TypeError, ValueError):
        raise WinterArcError("Invalid day.")
    if day < 1 or day > DAYS:
        raise WinterArcError("That day isn't part of the Winter Arc.")
    row = _row(user_id)
    if not row:
        # Not started yet: ONLY Day 1 is available, and completing it is what
        # starts the journey (records started_at on the server).
        if day != 1:
            raise WinterArcError("That day isn't unlocked yet — complete Day 1 to begin ✨", 423)
        now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        rewards = _rewards_for(1)
        with db.cursor() as c:
            c.execute(
                "INSERT INTO winter_arc (user_id, started_at, completed_days, earned_rewards, "
                "status, version, updated_at) VALUES (?,?,?,?,?,?,?)",
                (user_id, now, json.dumps([1]), json.dumps(rewards), "in_progress",
                 config.WINTER_ARC_VERSION, now),
            )
        return {"state": get_state(user_id), "newly_completed": True, "new_rewards": rewards}
    avail = current_day(row["started_at"])
    if day > avail:
        raise WinterArcError("That day isn't unlocked yet — it opens soon ✨", 423)

    completed = set(json.loads(row["completed_days"] or "[]"))
    before_rewards = set(json.loads(row["earned_rewards"] or "[]"))
    if day in completed:
        return {"state": _state_from_row(row), "newly_completed": False, "new_rewards": []}

    completed.add(day)
    completed_list = sorted(completed)
    rewards = _rewards_for(len(completed_list))
    status = "completed" if len(completed_list) >= DAYS else "in_progress"
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.cursor() as c:
        c.execute(
            "UPDATE winter_arc SET completed_days=?, earned_rewards=?, status=?, updated_at=? "
            "WHERE user_id=?",
            (json.dumps(completed_list), json.dumps(rewards), status, now, user_id),
        )
    new_rewards = [r for r in rewards if r not in before_rewards]
    state = get_state(user_id)
    return {"state": state, "newly_completed": True, "new_rewards": new_rewards}


def auto_complete(user_id: int, feature: str):
    """Event-driven completion: when a user actually performs a qualifying action
    (a successful meal scan -> 'scan', an AI-Coach message -> 'coach', a logged
    meal -> 'scan', …), complete the EARLIEST available, not-yet-done challenge
    whose `feature` matches.

    Safe by design: only acts on a STARTED arc, completes at most ONE task per
    event, is idempotent (re-processing the same event is a no-op once that task
    is done), and never touches unrelated tasks. Returns the completion dict or
    None. Callers wrap it so it can never break the underlying action."""
    if not feature:
        return None
    row = _row(user_id)
    if not row:
        return None                      # not started — Day 1 intention comes first
    avail = current_day(row["started_at"])
    completed = set(json.loads(row["completed_days"] or "[]"))
    for ch in CHALLENGES:
        if ch["day"] <= avail and ch["day"] not in completed and ch.get("feature") == feature:
            try:
                return complete_day(user_id, ch["day"])
            except WinterArcError:
                return None
    return None
