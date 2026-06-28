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


# ---------------- subscription management ----------------
def subscriptions() -> dict:
    with db.cursor() as c:
        rows = c.execute(
            "SELECT email, name, plan, subscription_status, stripe_customer, created_at "
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
             "since": (r["created_at"] or "")[:10]}
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

    mrr = round(paying * config.MRR_PER_SUBSCRIBER, 2)
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
