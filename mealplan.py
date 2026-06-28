"""Local, database-driven meal-plan engine — no OpenAI.

Selects real meal templates from food_db, filters by dietary preference and
allergies/exclusions, scales each to the user's calorie target, and returns the
SAME response shape the frontend already renders (so the UI is unchanged):

  { targets, modes, meals:[{slot,title,description,calories,protein,carbs,
    fat,fiber,ingredients:[{item,grams}],steps[]}], shopping_list[], images_enabled }
"""
from __future__ import annotations

import food_db
import images

SLOTS = [("Breakfast", 0.25), ("Lunch", 0.30), ("Snack", 0.15), ("Dinner", 0.30)]
_SCALE_MIN, _SCALE_MAX = 0.6, 1.8


def _passes(t: dict, modes: list[str], exclusions: list[str]) -> bool:
    for m in modes:
        if m in food_db.DIET_FLAGS and m not in t["diets"]:
            return False
    for ex in exclusions:
        if ex in t["allergens"]:
            return False
        if any(ex in ing["item"].lower() for ing in t["ingredients"]):
            return False
    return True


def _scale(t: dict, target_kcal: int, slot: str) -> dict:
    base = t["kcal"] or target_kcal or 1
    s = max(_SCALE_MIN, min(_SCALE_MAX, target_kcal / base))
    return {
        "slot": slot,
        "title": t["title"],
        "description": t["description"],
        "calories": round(t["kcal"] * s),
        "protein": round(t["protein"] * s, 1),
        "carbs": round(t["carbs"] * s, 1),
        "fat": round(t["fat"] * s, 1),
        "fiber": round(t["fiber"] * s, 1),
        "ingredients": [{"item": i["item"], "grams": max(1, round(i["grams"] * s))}
                        for i in t["ingredients"] if i["grams"]],
        "steps": list(t["steps"]),
    }


def _norm_exclusions(exclusions):
    return [str(e).strip().lower() for e in (exclusions or []) if str(e).strip()]


def generate_plan(targets: dict, modes: list[str], exclusions=None, offset: int = 0) -> dict:
    modes = modes or []
    exclusions = _norm_exclusions(exclusions)
    cals = targets.get("calories", 1800)

    meals, shopping = [], []
    for i, (slot, pct) in enumerate(SLOTS):
        target_kcal = round(cals * pct)
        pool = [t for t in food_db.templates_for(slot) if _passes(t, modes, exclusions)]
        if not pool:  # exclusions/modes too strict for this slot — relax exclusions
            pool = [t for t in food_db.templates_for(slot) if _passes(t, modes, [])]
        if not pool:
            pool = food_db.templates_for(slot)
        # Rotate by a per-generation offset (+ slot spread) for variety — each
        # new plan surfaces different meals instead of repeating the same set.
        meal = _scale(pool[(offset + i * 3) % len(pool)], target_kcal, slot)
        meals.append(meal)
        for ing in meal["ingredients"]:
            if ing["item"] not in shopping:
                shopping.append(ing["item"])

    return {
        "targets": targets,
        "modes": modes,
        "meals": meals,
        "shopping_list": sorted(shopping),
        "images_enabled": images.available(),
        "engine": "local",
    }


def regenerate_meal(slot: str, target_calories: int, modes: list[str], avoid: str = "") -> dict:
    modes = modes or []
    pool = [t for t in food_db.templates_for(slot) if _passes(t, modes, [])]
    if not pool:
        pool = food_db.templates_for(slot)
    candidates = [t for t in pool if t["title"] != avoid] or pool
    # deterministic but different from the avoided meal
    idx = (abs(hash(avoid)) % len(candidates)) if avoid else 0
    return _scale(candidates[idx], int(target_calories or 400), slot)
