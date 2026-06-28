"""AI Coach Chat — a virtual Paullina Ruban coach (premium feature).

Holds a short rolling conversation per user (persisted in SQLite so history
survives reloads) and generates supportive, realistic coaching replies via
GPT-4o. The coach can adapt workouts on the fly ("I only have dumbbells",
"create a 20-minute glute workout", "I missed this week").
"""
from __future__ import annotations

import json

import db
import llm

PERSONA = (
    "You are the Supermodel Wellness Coach — a premium, luxury wellness mentor for "
    "women, created for Paullina Ruban's Supermodel Wellness Club. You act as a "
    "nutritionist, wellness mentor, habit coach and lifestyle coach in one. You can "
    "help with ANY wellness topic: nutrition, weight & fat loss, healthy habits, "
    "exercise & fitness, motivation & mindset, lifestyle, travel wellness, bloating, "
    "digestion, meal planning, supplements, sleep, stress and healthy routines.\n\n"
    "Your values: sustainable habits, long-term success, balance and consistency over "
    "perfection, and evidence-based guidance. You NEVER shame the user, never use "
    "guilt-based language, never encourage crash diets, extreme restriction, or any "
    "unhealthy eating behaviour, and never make unrealistic promises.\n\n"
    "Your tone is supportive, professional, encouraging, confident, calm and "
    "practical — like a high-end wellness mentor who genuinely cares. Keep replies "
    "concise and warm (usually under 180 words). Always give actionable advice and "
    "clear next steps. If the user is struggling, reassure them first, then offer one "
    "small, doable step. When asked for a workout, give a clear routine with "
    "exercises, sets and reps. For anything medical, an injury, pregnancy, a possible "
    "eating disorder, or supplements with real risks, gently recommend they consult a "
    "qualified professional.\n\n"
    "Stay strictly within wellness, health, fitness, nutrition, mindset and "
    "lifestyle. If the user asks about anything unrelated (e.g. general trivia, "
    "news, politics, coding, celebrities), do NOT answer it — warmly say it's "
    "outside what you help with and steer back to their wellness goals in one line."
)

_HISTORY_TURNS = 6  # recent messages fed back as context (caps token cost per reply)


def _ensure_table():
    with db.cursor() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS coach_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_coach_user ON coach_messages(user_id)")


def history(user_id: int) -> list:
    _ensure_table()
    with db.cursor() as c:
        rows = c.execute(
            "SELECT role, content FROM coach_messages WHERE user_id = ? ORDER BY id ASC LIMIT 200",
            (user_id,),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def _add(user_id: int, role: str, content: str):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO coach_messages (user_id, role, content) VALUES (?,?,?)",
            (user_id, role, content),
        )


def reply(user_id: int, message: str, profile: dict | None = None) -> str:
    _ensure_table()
    message = (message or "").strip()[:1000]
    if not message:
        raise llm.LLMError("Empty message.")

    convo = history(user_id)[-_HISTORY_TURNS:]
    convo.append({"role": "user", "content": message})

    system = PERSONA
    if profile:
        system += (
            f"\n\nClient context — goal: {profile.get('goal','(unset)')}, "
            f"level: {profile.get('activity','(unset)')}, gender: {profile.get('gender','female')}."
        )

    answer = llm.chat_text(system, convo, temperature=0.8, user_id=user_id, kind="coach")
    _add(user_id, "user", message)
    _add(user_id, "assistant", answer)
    return answer
