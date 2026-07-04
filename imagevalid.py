"""Strict validation for user-supplied image data URLs.

Only base64-encoded raster images with a safe MIME type are accepted. SVG is
deliberately refused because it can carry executable script. Malformed base64,
empty payloads, and oversized blobs are rejected. Used for meal-scan uploads,
community post images, and profile avatars.
"""
from __future__ import annotations

import base64
import re

# Safe raster types only — intentionally excludes image/svg+xml (scriptable).
_ALLOWED_SUBTYPES = {"png", "jpeg", "jpg", "gif", "webp"}
_DATA_URL_RE = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)

# ~8 MB of base64 text (a generous compressed photo). Also bounded by the
# server's MAX_BODY on the whole request; this caps a single image field.
MAX_DATA_URL_LEN = 8 * 1024 * 1024


def is_valid_image_data_url(value, max_len: int = MAX_DATA_URL_LEN) -> bool:
    """True only for 'data:image/<safe-type>;base64,<valid-base64>'."""
    if not isinstance(value, str) or not value or len(value) > max_len:
        return False
    m = _DATA_URL_RE.match(value)
    if not m:
        return False
    if m.group(1).lower() not in _ALLOWED_SUBTYPES:
        return False
    try:
        # validate=True rejects any non-base64 character (spaces, quotes, etc.).
        raw = base64.b64decode(m.group(2), validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    return len(raw) > 0


def clean_image_data_url(value):
    """Return the value if it's a valid image data URL, else None (for optional
    fields that should silently drop invalid input)."""
    return value if is_valid_image_data_url(value) else None
