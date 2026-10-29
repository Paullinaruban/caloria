"""AI Coach Chat — a virtual Paullina Ruban coach (premium feature).

Holds a short rolling conversation per user (persisted in SQLite so history
survives reloads) and generates supportive, realistic coaching replies via
GPT-4o. The coach can adapt workouts on the fly ("I only have dumbbells",
"create a 20-minute glute workout", "I missed this week").
"""
from __future__ import annotations

import base64
import io
import json

import config
import db
import llm

try:
    from PIL import Image
    _PIL = True
except Exception:  # noqa: BLE001 — Pillow should be present; degrade gracefully if not
    _PIL = False

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
        # Optional attached image (the OPTIMIZED data URL only — never the
        # original high-res upload). Additive migration.
        db._add_column(c, "coach_messages", "image", "TEXT")


def history(user_id: int) -> list:
    _ensure_table()
    with db.cursor() as c:
        rows = c.execute(
            "SELECT role, content, image FROM coach_messages WHERE user_id = ? ORDER BY id ASC LIMIT 200",
            (user_id,),
        ).fetchall()
    out = []
    for r in rows:
        m = {"role": r["role"], "content": r["content"]}
        img = None
        try:
            img = r["image"]
        except (KeyError, IndexError):
            img = None
        if img:
            m["image"] = img
        out.append(m)
    return out


def _add(user_id: int, role: str, content: str, image: str = None):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO coach_messages (user_id, role, content, image) VALUES (?,?,?,?)",
            (user_id, role, content, image),
        )


class ImageError(ValueError):
    pass


def optimize_image(data_url: str) -> str:
    """Downscale + re-encode an uploaded image for cheap vision input: longest
    side <= COACH_IMAGE_MAX_DIM, JPEG at COACH_IMAGE_QUALITY, EXIF stripped.
    Returns a `data:image/jpeg;base64,...` URL. Raises ImageError on bad input."""
    if not _PIL:
        # Without Pillow we cannot safely optimize; refuse rather than forward a
        # full-res image to the model.
        raise ImageError("Image processing is unavailable right now.")
    try:
        head, b64 = data_url.split(",", 1)
        raw = base64.b64decode(b64, validate=True)
    except Exception as e:  # noqa: BLE001
        raise ImageError("That image couldn't be read.") from e
    if len(raw) > config.COACH_IMAGE_MAX_BYTES:
        raise ImageError("That image is too large. Please choose a smaller photo.")
    try:
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")                     # flattens alpha, drops EXIF orientation data
        im.thumbnail((config.COACH_IMAGE_MAX_DIM, config.COACH_IMAGE_MAX_DIM), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=config.COACH_IMAGE_QUALITY, optimize=True)  # no exif passed => stripped
    except Exception as e:  # noqa: BLE001
        raise ImageError("That image couldn't be processed.") from e
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def reply(user_id: int, message: str, profile: dict | None = None,
          image_data_url: str | None = None) -> str:
    _ensure_table()
    message = (message or "").strip()[:1000]
    has_image = bool(image_data_url)
    if not message and not has_image:
        raise llm.LLMError("Empty message.")

    system = PERSONA
    if profile:
        system += (
            f"\n\nClient context — goal: {profile.get('goal','(unset)')}, "
            f"level: {profile.get('activity','(unset)')}, gender: {profile.get('gender','female')}."
        )

    # Past turns are fed back as TEXT only — old images are NEVER resent, so a
    # photo is paid for exactly once.
    convo = [{"role": m["role"], "content": m["content"]}
             for m in history(user_id)[-_HISTORY_TURNS:]]

    optimized = None
    if has_image:
        optimized = optimize_image(image_data_url)        # raises ImageError on bad input
        parts = [{"type": "text",
                  "text": message or "Here's a photo — what can you tell me about it for my wellness goals?"},
                 {"type": "image_url", "image_url": {"url": optimized, "detail": "low"}}]
        convo.append({"role": "user", "content": parts})
        # Route the image message through the vision-capable model (low detail).
        answer = llm.chat_text(system, convo, temperature=0.8, user_id=user_id,
                               kind="coach_image", model=config.OPENAI_VISION_MODEL)
    else:
        convo.append({"role": "user", "content": message})
        answer = llm.chat_text(system, convo, temperature=0.8, user_id=user_id, kind="coach")

    _add(user_id, "user", message, image=optimized)       # store ONLY the optimized image
    _add(user_id, "assistant", answer)
    return answer
