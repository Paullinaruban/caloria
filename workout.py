"""Local, rule-based workout engine — no OpenAI.

The user picks ONE category (glutes, full body, upper, lower, abs & core, cardio,
home) plus their goal, equipment/location, session length and fitness level, and
gets exactly ONE workout drawn from exercise_db: goal sets the sets/reps/rest
scheme, equipment decides which exercises are available (Gym vs Home differ),
session length sets the exercise count (and so total volume), and level filters
exercise difficulty. The active workout and completion history are persisted per
user. Premium-only feature.
"""
from __future__ import annotations

import json

import db
import exercise_db

# ---- goal → (sets, reps, rest) per exercise type ----
# Fat loss: higher volume, shorter rest. Hypertrophy goals: lower reps, more
# rest, progressive overload. Maintenance: balanced & sustainable.
GOAL_RULES = {
    "fat_loss": {
        "compound": (3, "12-15", "40s"), "isolation": (3, "15-20", "30s"),
        "core": (3, "20 reps", "30s"), "conditioning": (4, "40s", "20s"),
    },
    "glute_growth": {
        "compound": (4, "8-12", "75s"), "isolation": (3, "12-15", "60s"),
        "core": (3, "15 reps", "45s"), "conditioning": (3, "30s", "45s"),
    },
    "muscle_growth": {
        "compound": (4, "8-12", "90s"), "isolation": (3, "10-15", "60s"),
        "core": (3, "15 reps", "45s"), "conditioning": (2, "30s", "45s"),
    },
    "maintenance": {
        "compound": (3, "10-12", "60s"), "isolation": (3, "12-15", "45s"),
        "core": (3, "15 reps", "40s"), "conditioning": (2, "40s", "30s"),
    },
}


def _format(ex, goal):
    rules = GOAL_RULES[goal]
    sets, reps, rest = rules.get(ex["type"], rules["compound"])
    return {
        "name": ex["name"],
        "image": ex["image"],
        "instructions": ex["instructions"],
        "sets": sets,
        "reps": reps,
        "rest": rest,
        "target_muscles": ex["primary_muscles"] + ex["secondary_muscles"],
        "primary_muscles": ex["primary_muscles"],
        "benefits": ex["benefits"],
        "common_mistakes": ex["common_mistakes"],
        "difficulty": ex["difficulty"],
        "type": ex["type"],
    }


# ---------- persistence ----------
def _ensure_table():
    with db.cursor() as c:
        # Honest completion signal — one row per day the user confirms a workout.
        c.execute(
            """CREATE TABLE IF NOT EXISTS workout_completions (
                user_id    INTEGER NOT NULL,
                date       TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, date)
            )"""
        )
        # The user's ONE current workout (replaced each time they generate).
        c.execute(
            """CREATE TABLE IF NOT EXISTS active_workout (
                user_id    INTEGER PRIMARY KEY,
                data_json  TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # Completed workouts (history).
        c.execute(
            """CREATE TABLE IF NOT EXISTS workout_history (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                title        TEXT,
                category     TEXT,
                data_json    TEXT NOT NULL,
                completed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_wo_hist_user ON workout_history(user_id)")


# ---------- completion (real user-confirmed signal) ----------
def completion_count(user_id: int) -> int:
    _ensure_table()
    with db.cursor() as c:
        return c.execute("SELECT COUNT(*) n FROM workout_completions WHERE user_id=?",
                         (user_id,)).fetchone()["n"]


def completion_days(user_id: int) -> set:
    _ensure_table()
    with db.cursor() as c:
        rows = c.execute("SELECT date FROM workout_completions WHERE user_id=?", (user_id,)).fetchall()
    return {r["date"] for r in rows}


def completed_today(user_id: int) -> bool:
    today = __import__("datetime").date.today().isoformat()
    return today in completion_days(user_id)


# ---------- single-workout generator (one request = one workout) ----------
import random as _random

# The 7 user-facing categories → exercise_db pools + sourcing rules.
CATEGORY_DEF = {
    "glutes":     {"title": "Glutes",       "cats": ["glutes"],                         "equipment": "gym"},
    "full_body":  {"title": "Full Body",    "cats": ["full", "glutes", "upper", "legs", "core"], "equipment": "gym"},
    "upper_body": {"title": "Upper Body",   "cats": ["upper"],                          "equipment": "gym"},
    "lower_body": {"title": "Lower Body",   "cats": ["legs", "glutes"],                 "equipment": "gym"},
    "abs_core":   {"title": "Abs & Core",   "cats": ["core"],                           "equipment": "gym"},
    "cardio":     {"title": "Cardio",       "cats": None, "equipment": "bodyweight", "conditioning": True},
    "home":       {"title": "Home Workout", "cats": ["full", "glutes", "upper", "legs", "core"], "equipment": "home"},
}
_CATEGORY_SUMMARY = {
    "glutes": "A focused glute session — build, shape and strengthen.",
    "full_body": "A balanced full-body session hitting every major muscle group.",
    "upper_body": "Sculpt your arms, shoulders, back and chest.",
    "lower_body": "Strong legs and glutes — the foundation of everything.",
    "abs_core": "Core strength and definition from every angle.",
    "cardio": "Heart-pumping conditioning to build stamina and burn.",
    "home": "No equipment needed — a complete session you can do anywhere.",
}


# Session length (minutes) → number of exercises. Longer sessions = more
# exercises = more total sets/volume.
COUNT_BY_DURATION = {20: 4, 30: 6, 45: 7, 60: 9}


def _category_pool(catkey, level, equipment):
    """Exercises for a category, filtered by the user's equipment and fitness
    level (via exercise_db.select_pools). Gym unlocks machine/cable work that
    Home/bodyweight pools exclude, so Home vs Gym produce different exercises."""
    d = CATEGORY_DEF[catkey]
    pools = exercise_db.select_pools(equipment, level)
    out = []
    if d.get("conditioning"):
        for lst in pools.values():
            out += [ex for ex in lst if ex["type"] in ("conditioning", "compound")]
    else:
        for c in d["cats"]:
            out += pools.get(c, [])
    seen, uniq = set(), []
    for ex in out:
        if ex["name"] not in seen:
            seen.add(ex["name"]); uniq.append(ex)
    return uniq


def _last_exercise_names(user_id, catkey):
    with db.cursor() as c:
        row = c.execute(
            "SELECT data_json FROM workout_history WHERE user_id=? AND category=? "
            "ORDER BY id DESC LIMIT 1", (user_id, catkey)).fetchone()
    if not row:
        return set()
    try:
        return {e["name"] for e in (json.loads(row["data_json"]).get("exercises") or [])}
    except (TypeError, ValueError, KeyError):
        return set()


def generate_one(user_id: int, category: str, *, goal: str = "fat_loss",
                 equipment: str = "gym", duration: int = 30,
                 level: str = "intermediate") -> dict:
    """Generate exactly ONE workout for a category, honouring the user's
    configuration: goal (sets/reps/rest scheme), equipment/location (which
    exercises are available), session length (how many exercises → total volume)
    and fitness level (exercise difficulty). Sets it as the active workout."""
    _ensure_table()
    catkey = category if category in CATEGORY_DEF else "full_body"
    if goal not in GOAL_RULES:
        goal = "fat_loss"
    if level not in exercise_db.DIFFICULTY:
        level = "intermediate"
    if equipment not in exercise_db.AVAILABILITY:
        equipment = "gym"
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 30
    # The "Home Workout" preset is inherently home-equipment.
    eq = "home" if catkey == "home" else equipment

    pool = _category_pool(catkey, level, eq)
    # Never return an empty/too-thin workout: progressively widen difficulty, then
    # equipment, then category — only as a safety net for sparse combinations.
    if len(pool) < 4:
        pool = _category_pool(catkey, "advanced", eq) or pool
    if len(pool) < 4 and eq != "gym":
        pool = _category_pool(catkey, "advanced", "gym") or pool
    if not pool:
        pool = _category_pool("full_body", "advanced", "gym"); catkey = "full_body"

    # Session length → exercise count (capped by what the pool actually offers).
    target = COUNT_BY_DURATION.get(duration, 6)
    n = max(1, min(target, len(pool)))

    # Variety: avoid repeating the previous workout's exercises for this category.
    prev = _last_exercise_names(user_id, catkey)
    best = None
    for _ in range(6):
        pick = _random.sample(pool, n)
        overlap = len({e["name"] for e in pick} & prev)
        if best is None or overlap < best[0]:
            best = (overlap, pick)
        if overlap == 0:
            break
    exercises = [_format(ex, goal) for ex in best[1]]
    total_sets = sum(e["sets"] for e in exercises)
    workout = {
        "title": f"{CATEGORY_DEF[catkey]['title']} Workout",
        "category": catkey,
        "focus": CATEGORY_DEF[catkey]["title"],
        "summary": _CATEGORY_SUMMARY.get(catkey, ""),
        "duration_min": duration,
        "goal": goal,
        "equipment": eq,
        "level": level,
        "total_sets": total_sets,
        "exercises": exercises,
        "engine": "local",
    }
    with db.cursor() as c:
        c.execute(
            "INSERT INTO active_workout (user_id, data_json, created_at) VALUES (?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id) DO UPDATE SET data_json=excluded.data_json, created_at=CURRENT_TIMESTAMP",
            (user_id, json.dumps(workout)),
        )
    return workout


def active(user_id: int):
    """The user's current single workout, or None."""
    _ensure_table()
    with db.cursor() as c:
        row = c.execute("SELECT data_json FROM active_workout WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data_json"])
    except (TypeError, ValueError):
        return None


def complete(user_id: int) -> dict:
    """Complete the active workout: move it to history, log completion, clear active.

    The read of the active workout and the history/delete writes all happen inside
    ONE cursor() block so they run under a single hold of the global lock — this
    makes the whole operation atomic. Concurrent completes (multiple tabs, double
    clicks, API retries) therefore produce exactly one history row: the first call
    consumes the active workout, every later call finds none and is a no-op.
    """
    _ensure_table()
    today = __import__("datetime").date.today().isoformat()
    with db.cursor() as c:
        row = c.execute("SELECT data_json FROM active_workout WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            # Nothing active to complete (already completed, or never generated).
            n = c.execute("SELECT COUNT(*) n FROM workout_completions WHERE user_id=?", (user_id,)).fetchone()["n"]
            return {"ok": True, "completed": False, "total": n}
        try:
            wo = json.loads(row["data_json"])
        except (TypeError, ValueError):
            wo = {}
        c.execute(
            "INSERT INTO workout_history (user_id, title, category, data_json) VALUES (?,?,?,?)",
            (user_id, wo.get("title"), wo.get("category"), row["data_json"]),
        )
        c.execute("INSERT OR IGNORE INTO workout_completions (user_id, date) VALUES (?,?)", (user_id, today))
        c.execute("DELETE FROM active_workout WHERE user_id=?", (user_id,))
        n = c.execute("SELECT COUNT(*) n FROM workout_completions WHERE user_id=?", (user_id,)).fetchone()["n"]
    return {"ok": True, "completed": True, "total": n}


def history(user_id: int, limit: int = 50) -> list:
    _ensure_table()
    with db.cursor() as c:
        rows = c.execute(
            "SELECT title, category, completed_at FROM workout_history WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?", (user_id, int(limit))).fetchall()
    return [{"title": r["title"], "category": r["category"], "completed_at": r["completed_at"]} for r in rows]
