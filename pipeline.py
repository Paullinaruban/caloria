"""Orchestration: photo -> GPT-4o ingredients -> USDA nutrition -> totals.

This is the heart of the product. It produces the full structured result the
frontend renders: per-ingredient grams + macros + confidence, aggregate totals
(calories, protein, carbs, fat, fiber), an overall confidence score, and a
low-confidence flag.
"""
import config
import learning
import mealscore
import nutrition
import vision


def analyze_meal(image_data_url: str, user_id=None) -> dict:
    detection = vision.detect_ingredients(image_data_url, user_id=user_id)  # may raise VisionError

    ingredients_out = []
    totals = {
        "calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0,
        "fiber": 0.0, "sugar": 0.0, "sodium": 0.0,
    }
    conf_weights = []
    usda_warning = None
    usda_down = False  # circuit breaker: once USDA rate-limits, skip it for this scan

    for ing in detection["ingredients"]:
        # Apply the learned portion correction for this ingredient.
        learned = learning.factor_for(ing["name"])
        grams = round(ing["estimated_grams"] * learned, 1)

        food = None
        if not usda_down:
            try:
                food = nutrition.lookup(ing["fdc_query"] or ing["name"])
            except nutrition.NutritionError as e:
                food = None
                usda_warning = str(e)
                if "rate limit" in str(e).lower():
                    usda_down = True  # don't keep hammering a rate-limited key

        scale = grams / 100.0
        ai = ing.get("ai_macros") or {}
        # Treat a USDA match with no/zero energy as no match (fall back to AI).
        if food and not (food.get("kcal_100g") or 0):
            food = None
        if food:
            macros = {
                "calories": round(food["kcal_100g"] * scale),
                "protein": round(food["protein_100g"] * scale, 1),
                "carbs": round(food["carbs_100g"] * scale, 1),
                "fat": round(food["fat_100g"] * scale, 1),
                "fiber": round(food["fiber_100g"] * scale, 1),
                "sugar": round((food.get("sugar_100g") or 0) * scale, 1),
                "sodium": round((food.get("sodium_100g") or 0) * scale),  # mg
            }
            matched = True
            source = food["description"]
            item_conf = ing["confidence"]
        elif ai.get("kcal_100g"):
            # USDA had no match / was unavailable → use GPT-4o's per-100g estimate
            # so the scan still returns useful calories & macros (slightly lower conf).
            macros = {
                "calories": round(ai["kcal_100g"] * scale),
                "protein": round(ai["protein_100g"] * scale, 1),
                "carbs": round(ai["carbs_100g"] * scale, 1),
                "fat": round(ai["fat_100g"] * scale, 1),
                "fiber": round(ai["fiber_100g"] * scale, 1),
                "sugar": round(ai["sugar_100g"] * scale, 1),
                "sodium": round(ai["sodium_100g"] * scale),  # mg
            }
            matched = True
            source = "AI estimate"
            item_conf = min(ing["confidence"], 0.7)
        else:
            macros = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0,
                      "fiber": 0, "sugar": 0, "sodium": 0}
            matched = False
            source = None
            item_conf = min(ing["confidence"], 0.4)

        for k in totals:
            totals[k] += macros[k]
        conf_weights.append(item_conf)

        ingredients_out.append(
            {
                "name": ing["name"],
                "grams": grams,
                "ai_grams": ing["estimated_grams"],
                "learned_factor": round(learned, 2),
                "confidence": round(item_conf, 2),
                "matched": matched,
                "source": source,
                **macros,
            }
        )

    # Overall confidence: blend GPT-4o's self-report with per-item average.
    if conf_weights:
        item_avg = sum(conf_weights) / len(conf_weights)
        overall = round(0.5 * detection["overall_confidence"] + 0.5 * item_avg, 2)
    else:
        overall = 0.0

    for k in ("protein", "carbs", "fat", "fiber", "sugar"):
        totals[k] = round(totals[k], 1)
    totals["calories"] = round(totals["calories"])
    totals["sodium"] = round(totals["sodium"])  # mg

    # Meal quality score + supportive coaching insights (grounded in the totals).
    quality = mealscore.evaluate(detection["meal_name"], totals, ingredients_out, user_id=user_id)

    return {
        "meal_name": detection["meal_name"],
        "meal_type": detection["meal_type"],
        "ingredients": ingredients_out,
        "totals": totals,
        "quality": quality,
        "confidence": overall,
        "low_confidence": overall <= config.LOW_CONFIDENCE_THRESHOLD,
        # Only surface a warning if we genuinely couldn't produce nutrition at all
        # (USDA failed AND the AI fallback was empty → 0 kcal).
        "warning": usda_warning if totals["calories"] == 0 else None,
    }
