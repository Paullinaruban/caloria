"""Food recognition via OpenAI GPT-4o Vision.

Sends the meal photo to GPT-4o and asks for a strict-JSON breakdown of every
visible ingredient with an estimated portion weight and a confidence score.
Nutrition numbers are intentionally NOT requested here — those come from USDA
(see nutrition.py), so the model only does what vision models are good at:
identifying foods and estimating portions.
"""
import json
import socket
import ssl
import time
import urllib.request
import urllib.error

import aicost
import config

SYSTEM_PROMPT = (
    "You are a professional nutritionist's vision assistant. You analyse a "
    "single photo of food and identify every distinct, visible ingredient. "
    "You estimate each ingredient's cooked/as-served weight in grams using "
    "visual cues (plate size, utensils, typical serving sizes). You handle "
    "mixed meals, restaurant plates, homemade dishes, and packaged foods. "
    "You are careful and calibrated: when an item is ambiguous or partially "
    "hidden, you lower its confidence rather than guessing."
)

USER_PROMPT = (
    "Analyse this meal photo. Return ONLY JSON matching this schema:\n"
    "{\n"
    '  "meal_name": string,            // short human label, e.g. "Chicken & rice bowl"\n'
    '  "meal_type": "homemade" | "restaurant" | "packaged" | "mixed",\n'
    '  "ingredients": [\n'
    "    {\n"
    '      "name": string,             // specific food, e.g. "grilled chicken breast"\n'
    '      "estimated_grams": number,  // as-served weight in grams\n'
    '      "confidence": number,       // 0..1 for THIS item\n'
    '      "fdc_query": string,        // best search term for the USDA database\n'
    '      "kcal_100g": number,        // calories per 100g (your best nutrition estimate)\n'
    '      "protein_100g": number,     // grams protein per 100g\n'
    '      "carbs_100g": number,       // grams carbohydrate per 100g\n'
    '      "fat_100g": number,         // grams fat per 100g\n'
    '      "fiber_100g": number,       // grams fibre per 100g\n'
    '      "sugar_100g": number,       // grams sugar per 100g\n'
    '      "sodium_100g": number       // mg sodium per 100g\n'
    "    }\n"
    "  ],\n"
    '  "overall_confidence": number    // 0..1 for the whole estimate\n'
    "}\n"
    "Rules: list each ingredient separately (do not merge a composite dish into "
    "one line unless it is genuinely a single food). Use realistic gram weights. "
    "For the per-100g nutrition fields, give standard reference values for that "
    "food (these are a fallback when the nutrition database has no match). "
    "If you cannot identify any food, return an empty ingredients array with "
    "overall_confidence 0."
)


class VisionError(RuntimeError):
    pass


def detect_ingredients(image_data_url: str, *, user_id=None) -> dict:
    """Call GPT-4o Vision and return the parsed detection dict.

    Raises VisionError on configuration or API problems.
    """
    if not config.OPENAI_API_KEY:
        raise VisionError(
            "OPENAI_API_KEY is not set. Add it to backend/.env to enable AI analysis."
        )

    payload = {
        "model": config.OPENAI_VISION_MODEL,
        "temperature": 0.2,  # accuracy over creativity
        "max_tokens": 800,   # cap output — bounds the JSON response cost
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": USER_PROMPT},
                    {
                        # "low" detail = flat ~85 image tokens instead of ~765 for a
                        # tiled "high" image. The frontend already downscales the photo,
                        # and ~512px is enough to identify a plated meal.
                        "type": "image_url",
                        "image_url": {"url": image_data_url, "detail": "low"},
                    },
                ],
            },
        ],
    }

    req = urllib.request.Request(
        f"{config.OPENAI_BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    _t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=config.OPENAI_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        aicost.record_error("scan_vision", config.OPENAI_VISION_MODEL, (time.perf_counter()-_t0)*1000, user_id, "error", f"HTTP {e.code}")
        raise VisionError(f"OpenAI API error {e.code}: {detail}") from e
    except (ssl.SSLError, socket.timeout) as e:
        aicost.record_error("scan_vision", config.OPENAI_VISION_MODEL, (time.perf_counter()-_t0)*1000, user_id, "timeout", str(e))
        raise VisionError(f"Network error reaching OpenAI: {e}") from e
    except urllib.error.URLError as e:
        st = "timeout" if isinstance(getattr(e, "reason", None), socket.timeout) else "error"
        aicost.record_error("scan_vision", config.OPENAI_VISION_MODEL, (time.perf_counter()-_t0)*1000, user_id, st, str(e.reason))
        raise VisionError(f"Could not reach OpenAI: {e.reason}") from e
    except OSError as e:
        aicost.record_error("scan_vision", config.OPENAI_VISION_MODEL, (time.perf_counter()-_t0)*1000, user_id, "error", str(e))
        raise VisionError(f"Network error reaching OpenAI: {e}") from e

    aicost.record(body.get("usage"), body.get("model") or config.OPENAI_VISION_MODEL,
                  (time.perf_counter() - _t0) * 1000, user_id=user_id, kind="scan_vision")

    try:
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise VisionError("Unexpected response from GPT-4o") from e

    # Normalise / validate shape.
    ingredients = []
    for item in data.get("ingredients", []):
        try:
            grams = float(item.get("estimated_grams", 0) or 0)
        except (TypeError, ValueError):
            grams = 0.0
        if grams <= 0:
            continue
        def _num(key):
            try:
                return max(0.0, float(item.get(key, 0) or 0))
            except (TypeError, ValueError):
                return 0.0
        ingredients.append(
            {
                "name": str(item.get("name", "food")).strip()[:80] or "food",
                "estimated_grams": round(grams, 1),
                "confidence": _clamp(item.get("confidence", 0.5)),
                "fdc_query": str(item.get("fdc_query") or item.get("name") or "").strip()[:80],
                # AI per-100g nutrition estimate — used as a fallback when USDA
                # has no match or is unavailable, so the scan is never empty.
                "ai_macros": {
                    "kcal_100g": _num("kcal_100g"),
                    "protein_100g": _num("protein_100g"),
                    "carbs_100g": _num("carbs_100g"),
                    "fat_100g": _num("fat_100g"),
                    "fiber_100g": _num("fiber_100g"),
                    "sugar_100g": _num("sugar_100g"),
                    "sodium_100g": _num("sodium_100g"),
                },
            }
        )

    return {
        "meal_name": str(data.get("meal_name", "Meal")).strip()[:80] or "Meal",
        "meal_type": data.get("meal_type", "mixed"),
        "ingredients": ingredients,
        "overall_confidence": _clamp(data.get("overall_confidence", 0.5)),
    }


def _clamp(v, lo=0.0, hi=1.0):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.5
