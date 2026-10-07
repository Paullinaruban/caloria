"""Admin / business operations read-models & actions (owner-only).

Powers the admin dashboard: user management, subscription management, customer
support views, analytics (MRR, conversion, retention, growth) and a simple
business overview. Pure reporting + a few safe mutations; never exposed to users.

All access is gated in server.py behind _require_admin (email in ADMIN_EMAILS).
"""
from __future__ import annotations

import datetime

import config
import db
import usage

# Subscription statuses that represent real paying customers (for MRR).
_PAYING = ("active", "trialing")


def _month() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m")


def _issues(row, scans, coach, scan_lim, coach_lim) -> list:
    out = []
    if not row["active"]:
        out.append("Account deactivated")
    if not row["email_verified"]:
        out.append("Email not verified")
    if row["subscription_status"] == "past_due":
        out.append("Payment failed (past due)")
    if scan_lim and scans >= scan_lim:
        out.append("Scan limit reached")
    elif scan_lim and scans >= 0.75 * scan_lim:
        out.append("Approaching scan limit")
    if coach_lim and coach >= coach_lim:
        out.append("Coach limit reached")
    elif coach_lim and coach >= 0.75 * coach_lim:
        out.append("Approaching coach limit")
    return out


def _summary_row(c, r, period):
    u = c.execute(
        "SELECT scans, coach FROM usage WHERE user_id = ? AND period = ?",
        (r["id"], period),
    ).fetchone()
    scans = u["scans"] if u else 0
    coach = u["coach"] if u else 0
    return {
        "email": r["email"],
        "name": r["name"] or "",
        "plan": r["plan"],
        "verified": bool(r["email_verified"]),
        "active": bool(r["active"]),
        "subscription_status": r["subscription_status"] or "—",
        "scans": scans,
        "coach": coach,
        "joined": (r["created_at"] or "")[:10],
    }


# ---------------- user management ----------------
def search_users(q: str = "", limit: int = 100) -> list:
    period = _month()
    q = (q or "").strip().lower()
    with db.cursor() as c:
        if q:
            rows = c.execute(
                "SELECT * FROM users WHERE LOWER(email) LIKE ? OR LOWER(name) LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (f"%{q}%", f"%{q}%", int(limit)),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [_summary_row(c, r, period) for r in rows]


def user_detail(email: str) -> dict:
    email = (email or "").strip().lower()
    period = _month()
    with db.cursor() as c:
        r = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not r:
            raise ValueError("No user with that email.")
        uid = r["id"]
        u = c.execute(
            "SELECT scans, coach FROM usage WHERE user_id = ? AND period = ?", (uid, period)
        ).fetchone()
        scans = u["scans"] if u else 0
        coach = u["coach"] if u else 0
        lim = c.execute(
            "SELECT scan_limit, coach_limit FROM user_limits WHERE user_id = ?", (uid,)
        ).fetchone()
        scan_lim = (lim["scan_limit"] if lim and lim["scan_limit"] is not None
                    else config.PREMIUM_SCAN_LIMIT)
        coach_lim = (lim["coach_limit"] if lim and lim["coach_limit"] is not None
                     else config.PREMIUM_COACH_LIMIT)
        meals = c.execute(
            "SELECT COUNT(*) n, MAX(created_at) last FROM meals WHERE user_id = ?", (uid,)
        ).fetchone()
        events = c.execute(
            "SELECT type, status, created_at FROM billing_events WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 20",
            (uid,),
        ).fetchall()

    import json
    profile = json.loads(r["profile_json"]) if r["profile_json"] else None
    return {
        "email": r["email"],
        "name": r["name"] or "",
        "plan": r["plan"],
        "verified": bool(r["email_verified"]),
        "active": bool(r["active"]),
        "joined": (r["created_at"] or "")[:10],
        "subscription_status": r["subscription_status"] or "—",
        "stripe_customer": r["stripe_customer"] or None,
        "stripe_subscription": r["stripe_subscription"] or None,
        "plan_interval": r["plan_interval"] or None,
        "subscribed_at": r["subscribed_at"] or None,
        "founding_member": bool(r["founding_member"]),
        "profile": profile,
        "usage": {
            "period": period,
            "scans": scans, "coach": coach,
            "scan_limit": scan_lim, "coach_limit": coach_lim,
            "lifetime_scans": r["scans_used"],
            "saved_meals": meals["n"], "last_meal": meals["last"],
        },
        "billing_history": [dict(e) for e in events],
        "issues": _issues(r, scans, coach, scan_lim, coach_lim),
    }


def entitlement_trace(email: str, fix: bool = False) -> dict:
    """Print the EXACT premium-access decision chain for one account, with the
    real database values and a LIVE Stripe lookup — so support can see precisely
    which field/condition is causing a paid customer to be gated, instead of
    guessing. With fix=True it also runs the Stripe reconcile (self-heal) and
    reports before/after. Admin-gated, read-only unless fix=True."""
    import auth
    import billing

    email = (email or "").strip().lower()
    with db.cursor() as c:
        r = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not r:
            raise ValueError("No user with that email.")
        # Duplicate / sibling accounts that could hold the real subscription.
        dupes = c.execute(
            "SELECT id, email, plan, subscription_status, stripe_customer, stripe_subscription "
            "FROM users WHERE id <> ? AND (stripe_customer = ? OR LOWER(name) = LOWER(?)) ",
            (r["id"], r["stripe_customer"], r["name"] or ""),
        ).fetchall()

    status = str(r["subscription_status"] or "").lower()
    has_stripe = bool(r["stripe_subscription"] or r["stripe_customer"])
    status_entitled = status in auth._ENTITLED_STRIPE_STATUS
    iap = bool(r["iap_active"])
    admin_email = (r["email"] or "").lower() in config.ADMIN_EMAILS

    # The actual decision chain, step by step, with the value at each gate.
    chain = {
        "dev_unlimited": bool(config.DEV_UNLIMITED),
        "admin_email": admin_email,
        "plan_is_premium": r["plan"] == "premium",
        "has_stripe_link": has_stripe,
        "subscription_status": status or "—",
        "status_in_entitled_set": status_entitled,
        "entitled_set": sorted(auth._ENTITLED_STRIPE_STATUS),
        "iap_active": iap,
        "_has_live_entitlement": (has_stripe and status_entitled) or iap,
        "is_premium": auth.is_premium(r),
    }

    # The real Stripe truth for this exact subscription (not the cached column).
    stripe_live = {"checked": False}
    try:
        info = billing.subscription_info(r)  # does a live Stripe GET when configured
        active_live = billing.live_subscription_active(r)
        stripe_live = {
            "checked": True,
            "live_status": info.get("status"),
            "current_period_end": info.get("current_period_end"),
            "cancel_at_period_end": info.get("cancel_at_period_end"),
            "stripe_says_active": active_live,  # True/False/None(undeterminable)
        }
    except Exception as e:  # noqa: BLE001
        stripe_live = {"checked": False, "error": str(e)}

    result = {
        "user": {
            "id": r["id"], "email": r["email"], "name": r["name"] or "",
            "plan": r["plan"], "subscription_status": r["subscription_status"] or "—",
            "stripe_customer": r["stripe_customer"] or None,
            "stripe_subscription": r["stripe_subscription"] or None,
            "iap_active": iap, "email_verified": bool(r["email_verified"]),
            "active": bool(r["active"]), "created_at": r["created_at"],
        },
        "decision_chain": chain,
        "stripe_live": stripe_live,
        "duplicate_accounts": [dict(d) for d in dupes],
        "verdict": _entitlement_verdict(chain, stripe_live, dupes),
    }

    if fix:
        before = auth.is_premium(r)
        healed = False
        try:
            healed = billing.reconcile_entitlement(r)
        except Exception as e:  # noqa: BLE001
            result["fix_error"] = str(e)
        # Re-read after any repair.
        with db.cursor() as c:
            r2 = c.execute("SELECT * FROM users WHERE id = ?", (r["id"],)).fetchone()
        result["fix"] = {
            "attempted": True,
            "reconciled": healed,
            "is_premium_before": before,
            "is_premium_after": auth.is_premium(r2),
            "plan_after": r2["plan"],
            "subscription_status_after": r2["subscription_status"],
        }
    return result


def _entitlement_verdict(chain, stripe_live, dupes) -> str:
    if chain["is_premium"]:
        return "ENTITLED — this account already resolves to premium; the gate should not fire."
    if chain["plan_is_premium"]:
        return "ENTITLED via plan flag."
    sa = stripe_live.get("stripe_says_active")
    if sa is True:
        return ("STALE CACHE — Stripe says the subscription is ACTIVE but the cached "
                "plan/status are not entitled. Run with fix=1 (or let /api/me reconcile) to self-heal.")
    if sa is False:
        return ("GENUINE LAPSE — Stripe reports this subscription is NOT active. This account "
                "is correctly gated; it is a billing matter, not a code bug.")
    if dupes:
        return ("POSSIBLE WRONG ACCOUNT — no live Stripe subscription on THIS row, but "
                "sibling account(s) share the Stripe customer or name; the subscription may live there.")
    return ("NO STRIPE SUBSCRIPTION on this row and Stripe status undeterminable — likely the "
            "paying account is a different record, or billing never attached a subscription here.")


def set_active(email: str, active: bool) -> dict:
    email = (email or "").strip().lower()
    with db.cursor() as c:
        r = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not r:
            raise ValueError("No user with that email.")
        c.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, r["id"]))
        if not active:  # revoking access also kills live sessions
            c.execute("DELETE FROM sessions WHERE user_id = ?", (r["id"],))
    return user_detail(email)


def set_premium(email: str, on: bool) -> dict:
    """Manually grant/revoke premium (comp account — independent of Stripe)."""
    email = (email or "").strip().lower()
    with db.cursor() as c:
        r = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not r:
            raise ValueError("No user with that email.")
        if on:
            c.execute(
                "UPDATE users SET plan = 'premium', subscription_status = 'manual' WHERE id = ?",
                (r["id"],),
            )
        else:
            c.execute(
                "UPDATE users SET plan = 'free', subscription_status = 'canceled' WHERE id = ?",
                (r["id"],),
            )
    return user_detail(email)


def set_founding(email: str, on: bool) -> dict:
    """Manually grant/revoke the Founding Member badge (bypasses the invite list
    and window — for hand-picked additions or corrections)."""
    import datetime
    email = (email or "").strip().lower()
    with db.cursor() as c:
        r = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not r:
            raise ValueError("No user with that email.")
        if on:
            now = datetime.datetime.utcnow().isoformat() + "Z"
            c.execute(
                "UPDATE users SET founding_member = 1, "
                "founding_member_at = COALESCE(founding_member_at, ?) WHERE id = ?",
                (now, r["id"]),
            )
        else:
            c.execute(
                "UPDATE users SET founding_member = 0, founding_member_at = NULL WHERE id = ?",
                (r["id"],),
            )
    return user_detail(email)


# ---------------- subscription management ----------------
def subscriptions() -> dict:
    with db.cursor() as c:
        rows = c.execute(
            "SELECT email, name, plan, subscription_status, stripe_customer, "
            "stripe_subscription, plan_interval, subscribed_at, created_at "
            "FROM users WHERE subscription_status IS NOT NULL ORDER BY created_at DESC"
        ).fetchall()
        failed = c.execute(
            "SELECT u.email, b.type, b.status, b.created_at FROM billing_events b "
            "LEFT JOIN users u ON u.id = b.user_id "
            "WHERE b.type = 'invoice.payment_failed' ORDER BY b.id DESC LIMIT 50"
        ).fetchall()

    def bucket(statuses):
        return [
            {"email": r["email"], "name": r["name"] or "", "status": r["subscription_status"],
             "interval": r["plan_interval"] or "—",
             "purchased": (r["subscribed_at"] or "")[:10] or "—",
             "since": (r["created_at"] or "")[:10],
             "stripe_customer": r["stripe_customer"] or "",
             "stripe_subscription": r["stripe_subscription"] or ""}
            for r in rows if r["subscription_status"] in statuses
        ]

    return {
        "active": bucket(("active", "trialing", "manual")),
        "past_due": bucket(("past_due",)),
        "canceled": bucket(("canceled",)),
        "failed_payments": [dict(f) for f in failed],
    }


# ---------------- analytics ----------------
def analytics() -> dict:
    period = _month()
    with db.cursor() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        verified = c.execute("SELECT COUNT(*) n FROM users WHERE email_verified = 1").fetchone()["n"]
        premium = c.execute("SELECT COUNT(*) n FROM users WHERE plan = 'premium'").fetchone()["n"]
        paying = c.execute(
            "SELECT COUNT(*) n FROM users WHERE subscription_status IN (?, ?)", _PAYING
        ).fetchone()["n"]
        past_due = c.execute(
            "SELECT COUNT(*) n FROM users WHERE subscription_status = 'past_due'"
        ).fetchone()["n"]
        canceled = c.execute(
            "SELECT COUNT(*) n FROM users WHERE subscription_status = 'canceled'"
        ).fetchone()["n"]
        monthly_subs = c.execute(
            "SELECT COUNT(*) n FROM users WHERE subscription_status IN (?, ?) "
            "AND plan_interval = 'monthly'", _PAYING
        ).fetchone()["n"]
        yearly_subs = c.execute(
            "SELECT COUNT(*) n FROM users WHERE subscription_status IN (?, ?) "
            "AND plan_interval = 'yearly'", _PAYING
        ).fetchone()["n"]
        new_users = c.execute(
            "SELECT COUNT(*) n FROM users WHERE substr(created_at,1,7) = ?", (period,)
        ).fetchone()["n"]
        new_subs = c.execute(
            "SELECT COUNT(*) n FROM billing_events WHERE type = 'checkout.session.completed' "
            "AND substr(created_at,1,7) = ?",
            (period,),
        ).fetchone()["n"]
        ever_subscribed = c.execute(
            "SELECT COUNT(DISTINCT user_id) n FROM billing_events "
            "WHERE type = 'checkout.session.completed'"
        ).fetchone()["n"]
        growth = c.execute(
            "SELECT substr(created_at,1,7) ym, COUNT(*) n FROM users "
            "GROUP BY ym ORDER BY ym DESC LIMIT 6"
        ).fetchall()
        u = c.execute(
            "SELECT COALESCE(SUM(scans),0) s, COALESCE(SUM(coach),0) co, COUNT(*) act "
            "FROM usage WHERE period = ? AND (scans > 0 OR coach > 0)",
            (period,),
        ).fetchone()

    # Interval-accurate revenue, driven by the single pricing source of truth:
    # monthly subs bill config.MONTHLY_PRICE_USD/mo, yearly subs bill
    # config.YEARLY_PRICE_USD/yr. Subscribers whose interval predates this
    # tracking fall back to the flat per-subscriber estimate so MRR never dips.
    _MONTHLY_PRICE, _YEARLY_PRICE = float(config.MONTHLY_PRICE_USD), float(config.YEARLY_PRICE_USD)
    known = monthly_subs + yearly_subs
    unknown = max(0, paying - known)
    mrr = round(
        monthly_subs * _MONTHLY_PRICE
        + yearly_subs * (_YEARLY_PRICE / 12.0)
        + unknown * config.MRR_PER_SUBSCRIBER,
        2,
    )
    conversion = round(paying / total * 100, 1) if total else 0.0
    verified_pct = round(verified / total * 100, 1) if total else 0.0
    churn = round(canceled / ever_subscribed * 100, 1) if ever_subscribed else 0.0
    retention = round(100 - churn, 1) if ever_subscribed else 0.0
    return {
        "period": period,
        "total_users": total,
        "verified_users": verified,
        "verified_pct": verified_pct,
        "premium_users": premium,
        "paying_subscribers": paying,
        "monthly_subscribers": monthly_subs,
        "yearly_subscribers": yearly_subs,
        "yearly_revenue_booked": round(yearly_subs * 99.0, 2),
        "past_due": past_due,
        "canceled": canceled,
        "conversion_rate_pct": conversion,
        "mrr": mrr,
        "arr": round(mrr * 12, 2),
        "new_users_this_month": new_users,
        "new_subscribers_this_month": new_subs,
        "ever_subscribed": ever_subscribed,
        "retention_pct": retention,
        "churn_pct": churn,
        "growth": [{"month": g["ym"], "new_users": g["n"]} for g in reversed(growth)],
        "usage_this_month": {
            "scans": u["s"], "coach": u["co"], "active_users": u["act"],
        },
    }


# ---------------- community moderation ----------------
def community_moderation(limit: int = 100) -> dict:
    """Recent community posts and comments for moderation (view + delete).
    Safe if the community tables don't exist yet (returns empty lists)."""
    limit = max(1, min(int(limit or 100), 500))
    with db.cursor() as c:
        have = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        posts, comments = [], []
        if "posts" in have:
            posts = [dict(r) for r in c.execute(
                "SELECT p.id, p.type, p.text, p.image, p.created_at, "
                "u.email AS author_email, u.name AS author_name, "
                "(SELECT COUNT(*) FROM post_likes WHERE post_id=p.id) AS likes, "
                "(SELECT COUNT(*) FROM post_comments WHERE post_id=p.id) AS comments "
                "FROM posts p LEFT JOIN users u ON u.id=p.user_id "
                "ORDER BY p.id DESC LIMIT ?", (limit,)
            ).fetchall()]
        if "post_comments" in have:
            comments = [dict(r) for r in c.execute(
                "SELECT cm.id, cm.post_id, cm.text, cm.created_at, "
                "u.email AS author_email, u.name AS author_name "
                "FROM post_comments cm LEFT JOIN users u ON u.id=cm.user_id "
                "ORDER BY cm.id DESC LIMIT ?", (limit,)
            ).fetchall()]
    return {
        "posts": posts,
        "comments": comments,
        "post_count": len(posts),
        "comment_count": len(comments),
    }


# ---------------- simple business overview ----------------
def business_overview() -> dict:
    """Plain-language snapshot for a non-technical owner."""
    a = analytics()
    d = usage.dashboard()
    top = sorted(d["users"], key=lambda x: x["scans"] + x["coach"], reverse=True)[:5]
    return {
        "users": a["total_users"],
        "paying": a["paying_subscribers"],
        "mrr": a["mrr"],
        "conversion_rate_pct": a["conversion_rate_pct"],
        "new_users_this_month": a["new_users_this_month"],
        "most_active": [
            {"email": u["email"], "scans": u["scans"], "coach": u["coach"]} for u in top
            if (u["scans"] + u["coach"]) > 0
        ],
        "approaching_limit": d["approaching_limit"],
    }
