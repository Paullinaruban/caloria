"""Optional AI food photography via OpenAI's image API (DALL·E 3).

Disabled by default (ENABLE_MEAL_IMAGES). Generated URLs are cached by prompt
hash so a meal card's photo is produced at most once.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

import config
import db


class ImageError(RuntimeError):
    pass


def available() -> bool:
    return config.openai_ready() and config.ENABLE_MEAL_IMAGES


def generate(dish: str) -> str | None:
    """Return an image URL for a dish, or None if disabled/unavailable."""
    if not available():
        return None
    key = "img:" + hashlib.sha256(dish.lower().encode()).hexdigest()[:24]
    cached = db.kv_get(key)
    if cached:
        return cached

    prompt = (
        f"Overhead professional food photography of {dish}, on a clean light "
        "background, soft natural light, fresh, appetizing, magazine quality, "
        "shallow depth of field."
    )
    payload = {
        "model": config.OPENAI_IMAGE_MODEL,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
    }
    req = urllib.request.Request(
        f"{config.OPENAI_BASE_URL}/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.OPENAI_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        url = body["data"][0]["url"]
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, IndexError) as e:
        raise ImageError(f"Image generation failed: {e}") from e

    db.kv_set(key, url)
    return url
