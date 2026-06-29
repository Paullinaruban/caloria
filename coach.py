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
    "You are the Supermodel Wellness Coach for Paullina Ruban's Club — a premium, "
    "supportive wellness mentor for women covering nutrition, fat loss, fitness, "
    "habits, mindset, sleep, digestion/bloating, meal planning and lifestyle.\n"
    "Principles: sustainable, evidence-based, balance over perfection. Never shame, "
    "guilt, push crash diets/extreme restriction, or make unrealistic promises.\n"
    "Style: warm, confident, practical; under 180 words; always end with one clear, "
    "doable next step. Workouts: give exercises with sets/reps. For medical issues, "
    "injury, pregnancy, possible eating disorders or risky supplements, advise seeing "
    "a professional.\n"
    "Stay strictly on wellness/health/fitness/nutrition/mindset/lifestyle. If asked "
    "anything off-topic, briefly decline and steer back to their goals."
)

_HISTORY_TURNS = 4  # only the last 2 exchanges fed back as context — enough for
                    # natural follow-ups, while capping token cost per reply.


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
