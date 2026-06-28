"""Nutrition lookup via USDA FoodData Central, cached in SQLite.

For each ingredient we search FDC, take the best per-100g match, and cache it so
repeat ingredients don't re-hit the API. Values are normalised to "per 100g" so
they can be scaled to any portion weight.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import urllib.error

import config
import db

# USDA nutrient numbers (stable identifiers in the FDC dataset).
NUTRIENT_NUMBERS = {
    "kcal": {"1008", "2047", "2048"},  # Energy (kcal) / Atwater general / specific
    "protein": {"1003"},
    "carbs": {"1005"},  # Carbohydrate, by difference
    "fat": {"1004"},  # Total lipid (fat)
    "fiber": {"1079"},  # Fiber, total dietary
    "sugar": {"2000", "1063"},  # Sugars, total (incl. NLEA)
    "sodium": {"1093"},  # Sodium, Na (mg)
}
_LABELS = ("kcal", "protein", "carbs", "fat", "fiber", "sugar", "sodium")


class NutritionError(RuntimeError):
    pass


def _normalise(query: str) -> str:
    return " ".join(query.lower().split())


def lookup(query: str) -> dict | None:
    """Return per-100g nutrition for a food query, or None if no match.

    Shape: {fdc_id, description, data_type, kcal_100g, protein_100g,
            carbs_100g, fat_100g, fiber_100g}
    """
    key = _normalise(query)
    if not key:
        return None

    cached = _from_cache(key)
    if cached is not None:
        return cached

    result = _search_usda(key)
    if result is not None:
        _to_cache(key, result)
    return result


def _from_cache(key: str):
    with db.cursor() as c:
        row = c.execute("SELECT * FROM usda_cache WHERE query = ?", (key,)).fetchone()
    if not row:
        return None
    if row["fdc_id"] is None:  # cached negative result
        return None
    return {
        "fdc_id": row["fdc_id"],
        "description": row["description"],
        "data_type": row["data_type"],
        "kcal_100g": row["kcal_100g"],
        "protein_100g": row["protein_100g"],
        "carbs_100g": row["carbs_100g"],
        "fat_100g": row["fat_100g"],
        "fiber_100g": row["fiber_100g"],
        "sugar_100g": row["sugar_100g"],
        "sodium_100g": row["sodium_100g"],
    }


def _to_cache(key: str, r) -> None:
    with db.cursor() as c:
        c.execute(
            """INSERT OR REPLACE INTO usda_cache
               (query, fdc_id, description, data_type, kcal_100g, protein_100g,
                carbs_100g, fat_100g, fiber_100g, sugar_100g, sodium_100g)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                None if r is None else r["fdc_id"],
                None if r is None else r["description"],
                None if r is None else r["data_type"],
                None if r is None else r["kcal_100g"],
                None if r is None else r["protein_100g"],
                None if r is None else r["carbs_100g"],
                None if r is None else r["fat_100g"],
                None if r is None else r["fiber_100g"],
                None if r is None else r["sugar_100g"],
                None if r is None else r["sodium_100g"],
            ),
        )


def _post_search(query: str, require_all: bool):
    """POST /foods/search — more robust than GET for multi-value dataType."""
    payload = json.dumps(
        {
            "query": query,
            "dataType": config.USDA_DATA_TYPES,
            "pageSize": 5,
            "requireAllWords": require_all,
        }
    ).encode("utf-8")
    url = f"{config.USDA_BASE_URL}/foods/search?api_key={urllib.parse.quote(config.USDA_API_KEY)}"
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=config.USDA_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _search_usda(query: str):
    try:
        body = _post_search(query, require_all=True)
        foods = body.get("foods") or []
        if not foods:  # loosen matching before giving up
            foods = (_post_search(query, require_all=False).get("foods") or [])
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise NutritionError(
                "USDA rate limit hit (DEMO_KEY is limited). Add a free USDA_FDC_API_KEY."
            ) from e
        raise NutritionError(f"USDA API error {e.code}") from e
    except urllib.error.URLError as e:
        raise NutritionError(f"Could not reach USDA: {e.reason}") from e

    if not foods:
        return None

    # Prefer the first result with a usable energy value (Foundation/SR Legacy
    # rank first and carry full nutrients). Abridged FNDDS rows sometimes omit
    # values — fall back to the authoritative /food/{id} detail endpoint.
    best = None
    for food in foods:
        extracted = _extract(food)
        if extracted["kcal_100g"] > 0:
            return extracted
        best = best or extracted

    if best and best["fdc_id"]:
        detailed = _fetch_detail(best["fdc_id"])
        if detailed and detailed["kcal_100g"] > 0:
            return detailed
    return best


def _fetch_detail(fdc_id: int):
    """GET /food/{id} — authoritative full nutrient profile (per 100g)."""
    url = f"{config.USDA_BASE_URL}/food/{fdc_id}?api_key={urllib.parse.quote(config.USDA_API_KEY)}"
    try:
        with urllib.request.urlopen(url, timeout=config.USDA_TIMEOUT) as resp:
            food = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    return _extract(food)


def _extract(food) -> dict:
    """Pull the five nutrients we care about (per 100g) from an FDC food.

    Handles both response shapes: search results use {nutrientNumber, value};
    the /food/{id} detail endpoint nests them as {nutrient:{number}, amount}.
    """
    vals = {k: 0.0 for k in _LABELS}
    got_kcal = False
    for n in food.get("foodNutrients", []):
        nested = n.get("nutrient") or {}
        num = str(n.get("nutrientNumber") or nested.get("number") or "")
        value = n.get("value")
        if value is None:
            value = n.get("amount")
        if value is None:
            continue
        for label, numbers in NUTRIENT_NUMBERS.items():
            if num in numbers:
                # Prefer the first kcal-energy entry we see (1008 before Atwater).
                if label == "kcal":
                    if not got_kcal:
                        vals["kcal"] = float(value)
                        got_kcal = True
                else:
                    vals[label] = float(value)
    return {
        "fdc_id": food.get("fdcId"),
        "description": food.get("description", ""),
        "data_type": food.get("dataType", ""),
        "sugar_100g": round(vals["sugar"], 2),
        "sodium_100g": round(vals["sodium"], 2),
        "kcal_100g": round(vals["kcal"], 2),
        "protein_100g": round(vals["protein"], 2),
        "carbs_100g": round(vals["carbs"], 2),
        "fat_100g": round(vals["fat"], 2),
        "fiber_100g": round(vals["fiber"], 2),
    }
