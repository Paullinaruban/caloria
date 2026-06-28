"""Learning from user corrections.

When a user fixes a portion estimate, we store (predicted, corrected). Over time
we compute a per-ingredient correction factor = mean(corrected / predicted) and
apply it to future portion estimates for the same ingredient. This nudges the
system toward each user's real-world portions — the same idea leading apps use
to improve accuracy from feedback.
"""
from __future__ import annotations

import db

# Don't let a single wild correction dominate; clamp the learned factor.
_MIN_FACTOR, _MAX_FACTOR = 0.4, 2.5
_MIN_SAMPLES = 1


def _key(ingredient: str) -> str:
    return " ".join(ingredient.lower().split())


def record_corrections(items: list[dict]) -> int:
    """Persist a batch of {name/ingredient, predicted_grams, corrected_grams}."""
    saved = 0
    with db.cursor() as c:
        for it in items:
            name = _key(str(it.get("ingredient") or it.get("name") or ""))
            try:
                pred = float(it.get("predicted_grams"))
                corr = float(it.get("corrected_grams"))
            except (TypeError, ValueError):
                continue
            if not name or pred <= 0 or corr <= 0:
                continue
            c.execute(
                "INSERT INTO corrections (ingredient, predicted_grams, corrected_grams) VALUES (?,?,?)",
                (name, pred, corr),
            )
            saved += 1
    return saved


def factor_for(ingredient: str) -> float:
    """Return the learned multiplier for an ingredient (1.0 if none/insufficient)."""
    name = _key(ingredient)
    if not name:
        return 1.0
    with db.cursor() as c:
        rows = c.execute(
            "SELECT predicted_grams, corrected_grams FROM corrections WHERE ingredient = ?",
            (name,),
        ).fetchall()
    ratios = [
        r["corrected_grams"] / r["predicted_grams"]
        for r in rows
        if r["predicted_grams"]
    ]
    if len(ratios) < _MIN_SAMPLES:
        return 1.0
    factor = sum(ratios) / len(ratios)
    return max(_MIN_FACTOR, min(_MAX_FACTOR, factor))
