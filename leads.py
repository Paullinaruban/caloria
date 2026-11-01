"""Pre-account onboarding leads.

A *lead* is a prospect who has completed the 7-step assessment and given an
email, but has NOT created an account or paid yet. We persist the email +
assessment keyed by an unguessable lead id so:
  • results survive a refresh / closed tab / returning later, and
  • after payment the Stripe customer can be matched back to the assessment.

A lead is NOT an account: no password, no session, no entitlements, and the
email is NOT treated as verified (a syntactically valid address proves nothing).
The lead id is a secret capability token — whoever holds it may read/update that
lead. No sensitive data beyond the email + assessment answers is stored.

Everything here is additive and isolated from the existing users/auth/billing
tables, so it cannot affect existing accounts or subscriptions.
"""
from __future__ import annotations

import json
import re
import secrets

import db

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# status lifecycle: new -> results -> checkout -> paid -> claimed (| abandoned)
_VALID_STATUS = {"new", "results", "checkout", "paid", "claimed", "abandoned"}


class LeadError(ValueError):
    pass


def normalize_email(email) -> str:
    return (email or "").strip().lower() if isinstance(email, str) else ""


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def _new_id() -> str:
    return secrets.token_urlsafe(24)


def create_or_update(lead_id, email, profile: dict) -> dict:
    """Create a new lead (no id) or update an existing one (valid id). Stores the
    normalized email + assessment profile. Returns {id, email}. Raises LeadError
    on a syntactically invalid email or bad profile."""
    email = normalize_email(email)
    if not valid_email(email):
        raise LeadError("Please enter a valid email address.")
    if not isinstance(profile, dict):
        raise LeadError("Invalid assessment data.")
    pj = json.dumps(profile)[:20000]   # bound the stored blob
    with db.cursor() as c:
        row = None
        if isinstance(lead_id, str) and lead_id:
            row = c.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row:
            c.execute(
                "UPDATE leads SET email = ?, profile_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (email, pj, row["id"]),
            )
            lid = row["id"]
        else:
            lid = _new_id()
            c.execute(
                "INSERT INTO leads (id, email, profile_json, status, updated_at) "
                "VALUES (?,?,?, 'results', CURRENT_TIMESTAMP)",
                (lid, email, pj),
            )
    return {"id": lid, "email": email}


def get(lead_id) -> dict | None:
    """Return {id, email, profile, status} for a lead id, or None. The id is the
    secret, so holding it is authorization to read the lead."""
    if not isinstance(lead_id, str) or not lead_id:
        return None
    with db.cursor() as c:
        r = c.execute(
            "SELECT id, email, profile_json, status FROM leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
    if not r:
        return None
    try:
        profile = json.loads(r["profile_json"]) if r["profile_json"] else {}
    except Exception:  # noqa: BLE001
        profile = {}
    return {"id": r["id"], "email": r["email"], "profile": profile, "status": r["status"]}


def set_status(lead_id: str, status: str) -> None:
    if status not in _VALID_STATUS:
        return
    with db.cursor() as c:
        c.execute(
            "UPDATE leads SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, lead_id),
        )
