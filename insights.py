"""Behavior-change insight engine — the "understand WHY" layer of Caloria.

All rule-based and local (no OpenAI, no cost). Powers:
  • "Why You Might Be Craving This" + "Try This Instead"  (per logged food)
  • "Patterns We're Noticing"                              (behavioral trends)
  • "Glow-Up Insights"                                     (energy/bloating/etc.)
  • Identity-based motivation lines                        (you ARE becoming her)

Guardrails (enforced in the wording everywhere): never diagnose, never make a
medical claim — only possibilities, using "may be related to / could be
influenced by / often associated with."
"""
from __future__ import annotations

import datetime
import json

import db

# ---- craving-trigger detection ----
CRAVING_KEYWORDS = {
    "chocolate": ["chocolate", "cocoa", "brownie", "nutella", "truffle"],
    "candy": ["candy", "gummy", "sweets", "lollipop", "skittles", "haribo", "jelly"],
    "dessert": ["cake", "cookie", "donut", "doughnut", "pastry", "ice cream", "cheesecake",
                "pie", "muffin", "cupcake", "tiramisu", "pudding", "waffle"],
    "fast_food": ["burger", "mcdonald", "kfc", "nuggets", "hot dog", "pizza", "taco bell", "fast food"],
    "fried": ["fries", "fried", "chips", "crisps", "tempura", "onion ring", "nuggets"],
    "soda": ["soda", "cola", "coke", "pepsi", "energy drink", "fanta", "sprite"],
    "sugary_drink": ["frappuccino", "milkshake", "boba", "bubble tea", "sweet latte", "iced caramel"],
}

# "Try This Instead" — realistic, never restrictive, keeps the joy.
SWAPS = {
    "chocolate": ("Large chocolate bar", "Greek yogurt with a few squares of dark chocolate"),
    "candy": ("Handful of candy", "Frozen grapes or berries with a little dark chocolate"),
    "dessert": ("Slice of cake / pastry", "Protein mug-cake or Greek yogurt with honey & fruit"),
    "fast_food": ("Fast-food combo", "A protein bowl — lean protein, carbs & veg you actually enjoy"),
    "fried": ("Bag of chips", "Air-popped popcorn or roasted chickpeas for the same crunch"),
    "soda": ("Sugary soda", "Sparkling water with citrus, or a zero-sugar version"),
    "sugary_drink": ("Sugary coffee / sweet drink", "High-protein iced latte — milk, espresso & a touch of sweetener"),
}

# Rotating "possibility" prompts (we don't track sleep/stress, so these are
# always framed as gentle possibilities, never conclusions).
_LIFESTYLE_POSSIBILITIES = [
    "This craving may be related to poor sleep or lower energy today.",
    "Cravings like this are often associated with elevated stress.",
    "Emotional fatigue can increase the appeal of comfort foods.",
    "Dips in mood or energy could be influencing this craving.",
]


def _today_meals(user_id):
    """Today's already-logged meals (UTC), oldest first."""
    today = datetime.date.today().isoformat()
    with db.cursor() as c:
        rows = c.execute(
            "SELECT data_json, created_at FROM meals WHERE user_id = ? "
            "AND substr(created_at,1,10) = ? ORDER BY id",
            (user_id, today),
        ).fetchall()
    out = []
    for r in rows:
        try:
            m = json.loads(r["data_json"]) or {}
            m["_dt"] = datetime.datetime.strptime(str(r["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
            out.append(m)
        except (TypeError, ValueError):
            continue
    return out


def detect_category(food_name: str):
    t = (food_name or "").lower()
    for cat, words in CRAVING_KEYWORDS.items():
        if any(w in t for w in words):
            return cat
    return None


def craving_insight(user_id: int, food_name: str) -> dict:
    """Return {'is_craving', 'food', 'reasons', 'swap'} for a logged food."""
    cat = detect_category(food_name)
    if not cat:
        return {"is_craving": False}

    reasons = []
    meals = _today_meals(user_id)
    now = datetime.datetime.utcnow()
    protein_today = sum(float(m.get("protein") or 0) for m in meals)

    # Low protein earlier today.
    if now.hour >= 12 and protein_today < 40:
        reasons.append("Low protein intake earlier in the day may increase cravings for "
                       "calorie-dense foods.")
    # Long gap since last meal.
    if meals:
        gap_h = (now - meals[-1]["_dt"]).total_seconds() / 3600
        if gap_h >= 4:
            reasons.append("Large gaps between meals often increase the desire for "
                           "quick-energy foods.")
    elif now.hour >= 12:
        reasons.append("Going a long time without eating can heighten cravings for "
                       "quick-energy foods.")
    # Late-night.
    if now.hour >= 20 or now.hour < 5:
        reasons.append("Late-night cravings are often associated with winding down or "
                       "fatigue more than true hunger.")

    # Always offer one gentle lifestyle possibility (rotates by day + user).
    idx = (now.timetuple().tm_yday + user_id) % len(_LIFESTYLE_POSSIBILITIES)
    reasons.append(_LIFESTYLE_POSSIBILITIES[idx])

    instead, try_this = SWAPS.get(cat, ("This treat", "A protein-rich version you'll still enjoy"))
    return {
        "is_craving": True,
        "food": food_name,
        "category": cat,
        "reasons": reasons[:3],
        "swap": {"instead": instead, "try": try_this},
    }


# ---- Patterns We're Noticing (needs a little history) ----
def patterns(meals: list) -> list:
    """Behavioral trends. `meals` carry _day, _dt and macros. Min ~5 days."""
    day_set = {m["_day"] for m in meals}
    if len(day_set) < 5:
        return []  # not enough signal yet

    out = []
    # 1) sweet cravings vs protein on that day
    sweet_days, plain_days = {}, {}
    for m in meals:
        is_sweet = detect_category(m.get("name", "")) in ("chocolate", "candy", "dessert", "sugary_drink")
        bucket = sweet_days if is_sweet else plain_days
        bucket.setdefault(m["_day"], 0.0)
    day_protein = {}
    for m in meals:
        day_protein.setdefault(m["_day"], 0.0)
        day_protein[m["_day"]] += float(m.get("protein") or 0)
    if sweet_days:
        avg_sweet = sum(day_protein[d] for d in sweet_days) / len(sweet_days)
        avg_other = (sum(day_protein[d] for d in day_protein if d not in sweet_days)
                     / max(len(day_protein) - len(sweet_days), 1))
        if avg_other - avg_sweet > 10:
            out.append("You tend to crave sweet foods most often on lower-protein days.")

    # 2) highest-calorie meals timing
    if meals:
        top = sorted(meals, key=lambda m: float(m.get("calories") or 0), reverse=True)[:max(3, len(meals)//5)]
        avg_hour = sum(m["_dt"].hour for m in top) / len(top)
        if avg_hour >= 18:
            out.append("Your highest-calorie meals usually happen later in the evening.")
        gaps_late = [m for m in top if m["_dt"].hour >= 15]
        if len(gaps_late) >= len(top) * 0.6 and "evening" not in " ".join(out):
            out.append("Your largest meals tend to come after long stretches without eating.")

    # 3) weekday vs weekend consistency
    weekday_days = len({d for d in day_set if d.weekday() < 5})
    weekend_days = len({d for d in day_set if d.weekday() >= 5})
    wk_rate = weekday_days / max(len([d for d in day_set if d.weekday() < 5]) or 1, 1)
    if weekday_days and weekend_days:
        if weekday_days / 5.0 > weekend_days / 2.0:
            out.append("Your strongest consistency happens on weekdays.")
        elif weekend_days / 2.0 > weekday_days / 5.0:
            out.append("You show up most consistently on weekends.")

    # 4) breakfast → quality
    bf_days = {m["_day"] for m in meals if m["_dt"].hour < 11}
    if bf_days and len(bf_days) >= 3:
        bf_protein = sum(day_protein[d] for d in bf_days) / len(bf_days)
        non_bf = [d for d in day_protein if d not in bf_days]
        if non_bf:
            other_protein = sum(day_protein[d] for d in non_bf) / len(non_bf)
            if bf_protein - other_protein > 8:
                out.append("Your meal quality improves on the days you log breakfast.")
    return out[:4]


# ---- Glow-Up Insights (energy / satiety / bloating / recovery / consistency) ----
def glow_insights(meals: list, targets: dict, days_week: int) -> list:
    today = datetime.date.today()
    week_ago = today - datetime.timedelta(days=6)
    recent = [m for m in meals if m["_day"] >= week_ago]
    if not recent:
        return ["Log a few meals this week to unlock your Glow-Up Insights."]
    n = len(recent)
    avg = lambda k: sum(float(m.get(k) or 0) for m in recent) / n
    protein, fiber, sugar, sodium = avg("protein"), avg("fiber"), avg("sugar"), avg("sodium")
    target_p = float(targets.get("protein") or 100)

    out = []
    if days_week >= 4:
        out.append("Your current eating pattern appears supportive of stable energy.")
    if protein >= target_p / 3.2:
        out.append("Protein intake appears supportive of body-composition goals.")
    else:
        out.append("A little more protein could be supportive of satiety and recovery.")
    if fiber >= 6:
        out.append("Recent fibre intake may be supportive of digestion and fuller meals.")
    if sugar >= 25 or sodium >= 1400:
        out.append("Recent choices could be associated with a higher bloating risk.")
    else:
        out.append("Recent choices appear associated with a lower bloating risk.")
    if days_week >= 5:
        out.append("Your consistency this week is becoming a genuine healthy habit.")
    return out[:4]


# ---- Identity-based motivation ----
def identity_line(streak: int, days_tracked: int, improvement_pct: int) -> str:
    if streak >= 2:
        return f"You've chosen your future self {streak} days in a row."
    if days_tracked >= 14:
        return "Consistency is becoming part of who you are."
    if improvement_pct > 0:
        return "The gap between your goals and your habits is shrinking."
    if days_tracked >= 1:
        return "You're acting like the person you're becoming."
    return "Every choice you log is a vote for the woman you're becoming."
