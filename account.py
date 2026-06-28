"""Account deletion — permanent erasure of a user and all their personal data.

Used by self-service deletion (DELETE /api/me) and admin deletion. Removes the
user row plus every table that holds their personal data or content, and
cancels/removes any Stripe customer first so no orphaned billing record remains.

Note: the `corrections` table stores anonymous, aggregate portion-correction
factors with no user identifier, so there is no personal data to remove there.
"""
from __future__ import annotations

import billing
import coach
import community
import config
import db
import usage


def _ensure_all():
    """Make sure lazily-created tables exist before we delete from them."""
    usage.init()
    coach._ensure_table()
    community._ensure()


def delete_account(user_id: int, *, cancel_billing: bool = True) -> None:
    """Permanently delete the user and all associated personal data."""
    with db.cursor() as c:
        u = c.execute(
            "SELECT stripe_customer FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not u:
        raise ValueError("No such user.")

    # 1) Stripe cleanup first — deleting the customer cancels any subscription
    #    and prevents orphaned billing records. Best-effort: never block deletion.
    if cancel_billing and config.stripe_ready() and u["stripe_customer"]:
        try:
            billing.delete_customer(u["stripe_customer"])
        except Exception as e:  # noqa: BLE001
            print(f"[caloria] Stripe cleanup during deletion failed for user {user_id}: {e}")

    # 2) Remove all data, including community content authored by the user and
    #    interactions on it.
    _ensure_all()
    with db.cursor() as c:
        post_ids = [r["id"] for r in c.execute(
            "SELECT id FROM posts WHERE user_id = ?", (user_id,)).fetchall()]
        if post_ids:
            marks = ",".join("?" * len(post_ids))
            c.execute(f"DELETE FROM post_likes WHERE post_id IN ({marks})", post_ids)
            c.execute(f"DELETE FROM post_comments WHERE post_id IN ({marks})", post_ids)
        c.execute("DELETE FROM posts WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM post_likes WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM post_comments WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM follows WHERE follower_id = ? OR followee_id = ?",
                  (user_id, user_id))
        c.execute("DELETE FROM member_profiles WHERE user_id = ?", (user_id,))
        # Personal data: meals/progress, coaching, usage, limits, tokens, billing log.
        c.execute("DELETE FROM meals WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM coach_messages WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM usage WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM user_limits WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM account_tokens WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM billing_events WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        # Finally the account itself (removes all remaining PII).
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))


def delete_by_email(email: str) -> None:
    """Admin helper: delete the account with the given email."""
    email = (email or "").strip().lower()
    with db.cursor() as c:
        r = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not r:
            raise ValueError("No user with that email.")
        uid = r["id"]
    delete_account(uid)
