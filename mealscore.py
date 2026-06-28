"""Meal quality scoring + coaching insights (hybrid).

The numeric score is computed deterministically from the meal's REAL macro
totals (the same numbers shown in the UI), so the score is always consistent
with the data and never hallucinated. The qualitative coaching text — benefits,
areas for improvement and recommendations — is written by OpenAI for a warm,
educational tone, with a deterministic fallback when OpenAI is unavailable.

Returned block (attached to the analyze result as result["quality"]):
{
  "score": 8.7,                 # 0..10, one decimal
  "stars": 4.5,                 # 0..5 in 0.5 steps (for ⭐ display)
  "benefits":        [str, ...],
  "improvements":    [str, ...],
  "recommendations": [str, ...],
  "ai": true                    # whether the text came from OpenAI
}
"""
from __future__ import annotations

import json

import llm

# Ingredient keywords that signal vegetables / whole plants on the plate.
_VEG_WORDS = (
    "veg", "spinach", "kale", "broccoli", "tomato", "cucumber", "pepper",
    "lettuce", "greens", "arugula", "carrot", "zucchini", "onion", "mushroom",
    "cabbage", "asparagus", "beet", "salad", "avocado", "berries", "berry",
    "apple", "fruit", "lentil", "bean", "chickpea", "pea", "leaf",
)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def compute_score(totals: dict, ingredients: list) -> float:
    """Deterministic 0..10 quality score from the real macro totals."""
    cal = float(totals.get("calories") or 0)
    if cal <= 0:
        return 0.0
    protein = float(totals.get("protein") or 0)
    fiber = float(totals.get("fiber") or 0)
    sugar = float(totals.get("sugar") or 0)
    sodium = float(totals.get("sodium") or 0)  # mg
    fat = float(totals.get("fat") or 0)

    pct_protein = (protein * 4) / cal
    pct_fat = (fat * 9) / cal

    score = 3.0  # neutral base

    # Protein adequacy (share of calories).
    if pct_protein >= 0.30:
        score += 2.5
    elif pct_protein >= 0.22:
        score += 2.0
    elif pct_protein >= 0.15:
        score += 1.0

    # Fibre.
    if fiber >= 8:
        score += 2.0
    elif fiber >= 5:
        score += 1.5
    elif fiber >= 3:
        score += 1.0

    # Sugar (lower is better).
    if sugar <= 5:
        score += 1.0
    elif sugar <= 15:
        score += 0.5
    elif sugar >= 30:
        score -= 1.0

    # Sodium (lower is better), mg.
    if sodium and sodium <= 500:
        score += 1.0
    elif sodium and sodium <= 900:
        score += 0.5
    elif sodium >= 1500:
        score -= 1.0

    # Fat in a sensible band.
    if 0.20 <= pct_fat <= 0.40:
        score += 1.0
    elif pct_fat >= 0.55:
        score -= 0.5

    # Whole-plant presence on the plate.
    names = " ".join(str(i.get("name", "")).lower() for i in ingredients)
    if any(w in names for w in _VEG_WORDS):
        score += 1.5

    return round(_clamp(score, 1.0, 10.0), 1)


def _stars(score: float) -> float:
    """Map a 0..10 score to a 0..5 rating in 0.5 steps."""
    return round((score / 2.0) * 2) / 2.0


# Foods some people find gas/water-retention-producing (FODMAPs, cruciferous,
# dairy, carbonation, gluten). Used only for a gentle, non-medical heads-up.
_BLOAT_WORDS = (
    "bean", "lentil", "chickpea", "legume", "broccoli", "cauliflower", "cabbage",
    "brussels", "onion", "garlic", "leek", "milk", "cheese", "cream", "yogurt",
    "dairy", "soda", "carbonated", "sparkling", "bread", "wheat", "pasta",
    "apple", "pear", "whey", "ice cream", "pickle",
)


def _bloating(totals: dict, ingredients: list) -> dict:
    """Grounded, NON-medical bloating-risk heads-up from this meal's sodium/sugar
    and known gas/water-retention ingredients. Informational only — no diagnosis,
    no claims; phrased as 'may be associated with… for some people'."""
    sodium = float(totals.get("sodium") or 0)   # mg
    sugar = float(totals.get("sugar") or 0)      # g
    fiber = float(totals.get("fiber") or 0)      # g
    names = " ".join(str(i.get("name", "")).lower() for i in ingredients)
    triggers = sorted({w for w in _BLOAT_WORDS if w in names})
    pts = 0
    if sodium >= 1200: pts += 2
    elif sodium >= 800: pts += 1
    if sugar >= 30: pts += 1
    if fiber >= 12: pts += 1     # a sudden large fibre load can feel bloating for some
    pts += min(2, len(triggers))
    level = "Higher" if pts >= 3 else "Moderate" if pts >= 1 else "Low"
    if level == "Low":
        note = "Nothing here stands out as likely to cause bloating for most people."
    else:
        bits = []
        if sodium >= 800: bits.append("higher sodium (can hold water)")
        if sugar >= 30: bits.append("high sugar")
        if triggers: bits.append("foods some find gassy (" + ", ".join(triggers[:3]) + ")")
        reason = "; ".join(bits) or "this combination"
        note = (f"May be associated with bloating for some people due to {reason}. "
                "Individual tolerance varies.")
    return {"bloating_risk": level, "bloating_note": note}


def _fallback_text(totals: dict, score: float, ingredients: list) -> dict:
    """Rule-based benefits/improvements/recommendations (no OpenAI)."""
    cal = float(totals.get("calories") or 0) or 1
    protein = float(totals.get("protein") or 0)
    fiber = float(totals.get("fiber") or 0)
    sugar = float(totals.get("sugar") or 0)
    sodium = float(totals.get("sodium") or 0)
    fat = float(totals.get("fat") or 0)
    pct_protein = (protein * 4) / cal
    pct_fat = (fat * 9) / cal
    names = " ".join(str(i.get("name", "")).lower() for i in ingredients)
    has_veg = any(w in names for w in _VEG_WORDS)

    benefits, improvements, recs = [], [], []

    if pct_protein >= 0.22:
        benefits.append("High in protein — great for satiety and recovery")
    if fiber >= 5:
        benefits.append("Good fibre content to support digestion")
    if has_veg:
        benefits.append("Includes vegetables for vitamins and minerals")
    if 0.20 <= pct_fat <= 0.40:
        benefits.append("Balanced fats for steady energy")
    if sugar <= 10:
        benefits.append("Low in added sugar — stable energy")
    if not benefits:
        benefits.append("A reasonable base to build a balanced plate on")

    if pct_protein < 0.15:
        improvements.append("Protein is on the lower side")
        recs.append("Add a palm-sized protein source like chicken, eggs, tofu or Greek yogurt")
    if fiber < 3:
        improvements.append("Could use more fibre")
        recs.append("Add a handful of vegetables, berries or a tablespoon of seeds")
    if not has_veg:
        improvements.append("Light on vegetables")
        recs.append("Add a side of greens or colourful veg to boost micronutrients")
    if sugar >= 30:
        improvements.append("Sugar is quite high")
        recs.append("Swap some of the sweet elements for fruit or a smaller portion")
    if sodium >= 1500:
        improvements.append("Sodium is high")
        recs.append("Go easy on added salt and salty sauces next time")
    if not improvements:
        improvements.append("Nothing major — this is a well-rounded meal")
    if not recs:
        recs.append("Keep doing what you're doing — this is a solid, balanced choice")

    return {"benefits": benefits[:5], "improvements": improvements[:5], "recommendations": recs[:4]}


_SYSTEM = (
    "You are a warm, professional wellness nutritionist writing short feedback "
    "on a meal for a women's wellness app. Your tone is ALWAYS supportive, "
    "educational and encouraging — never judgmental, never shaming, never about "
    "guilt or restriction. You promote sustainable, balanced eating. You return "
    "only valid JSON."
)


def _ai_text(meal_name: str, totals: dict, score: float, ingredients: list, user_id=None) -> dict:
    names = ", ".join(str(i.get("name", "")) for i in ingredients if i.get("name"))[:400]
    user = (
        f"Meal: {meal_name}\n"
        f"Ingredients: {names or 'unknown'}\n"
        f"Per-meal nutrition — calories: {totals.get('calories')}, "
        f"protein: {totals.get('protein')} g, carbs: {totals.get('carbs')} g, "
        f"fat: {totals.get('fat')} g, fiber: {totals.get('fiber')} g, "
        f"sugar: {totals.get('sugar')} g, sodium: {totals.get('sodium')} mg.\n"
        f"A quality score of {score}/10 has already been assigned — do not change it, "
        "just explain it.\n\n"
        "Return ONLY JSON with this exact shape:\n"
        "{\n"
        '  "benefits": [string],         // 2-4 genuine positives about THIS meal\n'
        '  "improvements": [string],     // 1-3 gentle, optional areas to improve\n'
        '  "recommendations": [string]   // 1-3 practical, supportive next steps\n'
        "}\n"
        "Each item is a short phrase (max ~12 words). Base everything on the numbers "
        "above. Be honest but kind; if the meal is great, say so warmly."
    )
    data = llm.chat_json(_SYSTEM, user, temperature=0.5, max_tokens=320,
                         user_id=user_id, kind="scan_text")

    def _clean(key, limit):
        out = []
        for x in data.get(key, []) or []:
            s = str(x).strip()
            if s:
                out.append(s[:140])
        return out[:limit]

    benefits = _clean("benefits", 5)
    improvements = _clean("improvements", 5)
    recs = _clean("recommendations", 4)
    if not (benefits or improvements or recs):
        raise llm.LLMError("empty coaching text")
    return {"benefits": benefits, "improvements": improvements, "recommendations": recs}


def evaluate(meal_name: str, totals: dict, ingredients: list, user_id=None) -> dict:
    """Produce the full quality block. Never raises — always returns a result."""
    score = compute_score(totals, ingredients)
    block = {"score": score, "stars": _stars(score)}
    block.update(_bloating(totals, ingredients))

    try:
        text = _ai_text(meal_name, totals, score, ingredients, user_id=user_id)
        block.update(text)
        block["ai"] = True
    except Exception:  # noqa: BLE001 — any OpenAI/parse problem → graceful fallback
        block.update(_fallback_text(totals, score, ingredients))
        block["ai"] = False

    return block
