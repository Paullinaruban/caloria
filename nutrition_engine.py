"""Caloria — evidence-based nutrition engine (single source of truth).

Every calorie & macro target in the app flows from compute_targets() here
(auth.compute_targets delegates to it; the dashboard, meal planner and
onboarding preview all read the stored result). Built on standard sports-
nutrition / dietetics methodology:

  • BMR via the Mifflin-St Jeor equation
  • TDEE via activity multipliers
  • Goal-specific calorie adjustment
  • Protein from bodyweight (g/kg) within goal-appropriate ranges
  • Fat from an evidence-based minimum, remainder allocated to carbohydrate

Guarantees: calories are never dangerously low, macro splits are realistic,
no negative/impossible values, and the returned calories ALWAYS equal
protein*4 + carbs*4 + fat*9 exactly (internal consistency).
"""
from __future__ import annotations

import datetime

# Standard activity multipliers applied to BMR → TDEE.
ACTIVITY = {
    "sedentary": 1.2, "light": 1.375, "moderate": 1.55,
    "active": 1.725, "athlete": 1.9,
}

# Per nutrition goal:
#   adj        — calorie adjustment vs TDEE (deficit/surplus)
#   protein_kg — target protein (g per kg bodyweight)
#   p_min/p_max— allowed protein band (g/kg) for clamping
#   fat_kg     — target fat (g per kg) before the % floor
#   fat_pct_min— fat must be at least this fraction of calories (never too low)
GOALS = {
    "fat_loss":     {"adj": -0.20, "protein_kg": 2.1, "p_min": 1.8, "p_max": 2.4, "fat_kg": 0.9, "fat_pct_min": 0.22},
    "lean_toned":   {"adj": -0.08, "protein_kg": 2.0, "p_min": 1.8, "p_max": 2.2, "fat_kg": 0.9, "fat_pct_min": 0.23},
    "athletic_lean":{"adj": +0.03, "protein_kg": 1.8, "p_min": 1.6, "p_max": 2.2, "fat_kg": 0.8, "fat_pct_min": 0.20},
    "muscle_gain":  {"adj": +0.12, "protein_kg": 2.0, "p_min": 1.8, "p_max": 2.2, "fat_kg": 0.9, "fat_pct_min": 0.22},
    "maintenance":  {"adj": 0.00,  "protein_kg": 1.7, "p_min": 1.6, "p_max": 2.0, "fat_kg": 1.0, "fat_pct_min": 0.25},
}

# Safe minimum daily calories (validation floor) by biological sex.
CALORIE_FLOOR = {"male": 1500, "female": 1200, "other": 1300}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def normalize_goal(goal, physique=None) -> str:
    """Map the app's existing goal/physique selections to a nutrition goal.

    The onboarding stores `goal` (fat_loss / muscle_gain / maintenance /
    healthy_lifestyle) and a `physique` choice (Supermodel Lean / Victoria
    Inspired / Toned & Feminine / Athletic Glow-Up). We derive one of the five
    nutrition goals so each selection produces distinct, science-backed targets.
    """
    g = str(goal or "").strip().lower()
    ph = str(physique or "").strip().lower()

    # Direct match if a nutrition goal value is ever passed.
    if g in GOALS:
        if g == "fat_loss" and "athletic" in ph:
            return "athletic_lean"
        if g == "fat_loss" and "toned" in ph:
            return "lean_toned"
        return g

    # Legacy / app-specific goal values.
    if "muscle" in g:                       # muscle_gain, muscle_growth, glute_growth
        return "muscle_gain"
    if g in ("healthy_lifestyle", "healthy"):
        return "athletic_lean" if "athletic" in ph else "maintenance"

    # Fall back to physique when goal is missing/unknown.
    if "athletic" in ph:
        return "athletic_lean"
    if "toned" in ph:
        return "lean_toned"
    if "lean" in ph or "supermodel" in ph or "victoria" in ph:
        return "fat_loss"
    return "maintenance"


def compute_targets(profile: dict) -> dict:
    # ---- sanitise inputs (prevent impossible values) ----
    # Age is derived from birth_year (we no longer store age directly); fall back
    # to a stored age for legacy profiles, else a safe default.
    age_val = profile.get("age")
    if not age_val and profile.get("birth_year"):
        try:
            age_val = datetime.datetime.utcnow().year - int(profile["birth_year"])
        except (TypeError, ValueError):
            age_val = None
    age = _clamp(float(age_val or 25), 14, 90)
    weight = _clamp(float(profile.get("weight", 65) or 65), 35, 250)   # kg
    height = _clamp(float(profile.get("height", 165) or 165), 120, 220)  # cm
    gender = str(profile.get("gender", "female") or "female").lower()
    sex = "male" if gender.startswith("m") else ("other" if gender.startswith("o") else "female")
    activity = ACTIVITY.get(str(profile.get("activity", "light")).lower(), 1.375)
    goal_key = normalize_goal(profile.get("goal"), profile.get("physique"))
    g = GOALS[goal_key]

    # ---- BMR (Mifflin-St Jeor) ----
    sex_const = {"male": 5, "female": -161, "other": -78}[sex]
    bmr = 10 * weight + 6.25 * height - 5 * age + sex_const

    # ---- TDEE → goal-adjusted calories ----
    tdee = bmr * activity
    calories = tdee * (1 + g["adj"])
    # Validation: never below the safe floor; round to nearest 10.
    calories = max(CALORIE_FLOOR[sex], round(calories / 10) * 10)

    # ---- Protein: bodyweight-based, clamped to the goal's g/kg band ----
    protein = round(_clamp(weight * g["protein_kg"], weight * g["p_min"], weight * g["p_max"]))
    # Sanity cap so protein never dominates the plate (≤45% of calories).
    protein = min(protein, round(0.45 * calories / 4))

    # ---- Fat: evidence-based minimum (g/kg AND % of calories) ----
    fat = round(max(weight * g["fat_kg"], g["fat_pct_min"] * calories / 9))

    # ---- Carbs: remaining calories after protein & fat ----
    carbs = round((calories - protein * 4 - fat * 9) / 4)

    # If carbs went too low (heavy person / large deficit), recover them by
    # trimming fat toward its floor, then protein toward its band minimum.
    min_carbs = round(0.10 * calories / 4)
    if carbs < min_carbs:
        fat_floor = round(max(weight * 0.6, 0.20 * calories / 9))
        fat = max(fat_floor, fat - (min_carbs - carbs) * 4 // 9)
        carbs = round((calories - protein * 4 - fat * 9) / 4)
    if carbs < 0:
        protein = max(round(weight * g["p_min"]), protein + (carbs * 4) // 4)
        carbs = max(0, round((calories - protein * 4 - fat * 9) / 4))

    # ---- Consistency guarantee: displayed calories == macro calories ----
    calories = protein * 4 + carbs * 4 + fat * 9

    return {
        "calories": int(calories),
        "protein": int(protein),
        "carbs": int(carbs),
        "fat": int(fat),
        "goal": goal_key,  # informational; harmless for existing consumers
    }
