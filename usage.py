"""Server-side usage metering, monthly caps, owner alerts & admin overrides.

INTERNAL SAFEGUARD ONLY. Premium users experience the platform as unlimited —
nothing here is ever surfaced to a normal user. Counters, quotas and remaining
allowances are never returned on any user-facing endpoint.

How it works
------------
* Usage is tracked per (user_id, period) where period = current UTC month
  ("YYYY-MM"). Because the key includes the month, usage RESETS AUTOMATICALLY
  on the 1st — a new month simply starts a fresh row at zero. No cron needed.
* Premium (paying, non-admin) users are capped at PREMIUM_SCAN_LIMIT scans and
  PREMIUM_COACH_LIMIT coach messages per month.
* Admins (config.ADMIN_EMAILS) and DEV_UNLIMITED are EXEMPT from enforcement but
  still counted, so the dashboard reflects real activity.
* Owner alerts fire once each as a user crosses 50 / 75 / 90 / 100% of a cap.
  Alerts are stored (admin_alerts), logged, and optionally POSTed to
  config.ALERT_WEBHOOK_URL.
* Admins can, at runtime (no code changes):
    - raise a user's persistent monthly allowance (set_limit)
    - reset a user's current-month usage (reset_usage)
    - grant a temporary one-month bonus (grant_bonus)

Concurrency: every public function opens its own short-lived cursor (the global
db lock is non-reentrant) and never holds the lock across a network call — the
alert webhook is sent only after the cursor block closes.
"""
from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.request

import config
import db

_THRESHOLDS = (50, 75, 90, 100)


def current_period(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.utcnow()
    return now.strftime("%Y-%m")


def _ensure(c) -> None:
    c.execute(
        """CREATE TABLE IF NOT EXISTS usage (
            user_id     INTEGER NOT NULL,
            period      TEXT NOT NULL,
            scans       INTEGER NOT NULL DEFAULT 0,
            coach       INTEGER NOT NULL DEFAULT 0,
            scan_bonus  INTEGER NOT NULL DEFAULT 0,
            coach_bonus INTEGER NOT NULL DEFAULT 0,
            scan_alert  INTEGER NOT NULL DEFAULT 0,
            coach_alert INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, period)
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS user_limits (
            user_id     INTEGER PRIMARY KEY,
            scan_limit  INTEGER,
            coach_limit INTEGER
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS admin_alerts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id    INTEGER NOT NULL,
            email      TEXT,
            plan       TEXT,
            metric     TEXT,
            threshold  INTEGER,
            scans      INTEGER,
            coach      INTEGER,
            period     TEXT,
            seen       INTEGER NOT NULL DEFAULT 0
        )"""
    )


def init() -> None:
    with db.cursor() as c:
        _ensure(c)


# ---------------- limits ----------------
def _effective_limits(c, user_id: int, period: str) -> tuple[int, int]:
    """(scan_limit, coach_limit) = persistent allowance + this month's bonus."""
    scan_lim = config.PREMIUM_SCAN_LIMIT
    coach_lim = config.PREMIUM_COACH_LIMIT
    row = c.execute(
        "SELECT scan_limit, coach_limit FROM user_limits WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row:
        if row["scan_limit"] is not None:
            scan_lim = row["scan_limit"]
        if row["coach_limit"] is not None:
            coach_lim = row["coach_limit"]
    u = c.execute(
        "SELECT scan_bonus, coach_bonus FROM usage WHERE user_id = ? AND period = ?",
        (user_id, period),
    ).fetchone()
    if u:
        scan_lim += u["scan_bonus"] or 0
        coach_lim += u["coach_bonus"] or 0
    return scan_lim, coach_lim


def allowed(user_id: int, kind: str, *, is_admin: bool = False) -> bool:
    """True if the user may perform one more action of `kind` this month.

    `kind` is 'scans' or 'coach'. Admins / DEV_UNLIMITED are never blocked.
    """
    if is_admin or config.DEV_UNLIMITED:
        return True
    period = current_period()
    with db.cursor() as c:
        _ensure(c)
        scan_lim, coach_lim = _effective_limits(c, user_id, period)
        row = c.execute(
            "SELECT scans, coach FROM usage WHERE user_id = ? AND period = ?",
            (user_id, period),
        ).fetchone()
    scans = row["scans"] if row else 0
    coach = row["coach"] if row else 0
    return scans < scan_lim if kind == "scans" else coach < coach_lim


def _threshold_for(pct: float) -> int:
    level = 0
    for t in _THRESHOLDS:
        if pct >= t:
            level = t
    return level


def record(user_id: int, kind: str, *, email: str = "", plan: str = "",
           is_admin: bool = False) -> list:
    """Count one action, compute any newly-crossed alert thresholds, persist them.

    Returns the list of alert dicts that fired (also pushed to the webhook).
    Counting happens for everyone (dashboard); alerts are skipped for admins.
    """
    period = current_period()
    fired = []
    col = "scans" if kind == "scans" else "coach"
    alert_col = "scan_alert" if kind == "scans" else "coach_alert"
    with db.cursor() as c:
        _ensure(c)
        c.execute(
            "INSERT OR IGNORE INTO usage (user_id, period) VALUES (?, ?)", (user_id, period)
        )
        c.execute(
            f"UPDATE usage SET {col} = {col} + 1 WHERE user_id = ? AND period = ?",
            (user_id, period),
        )
        row = c.execute(
            "SELECT scans, coach, scan_alert, coach_alert FROM usage "
            "WHERE user_id = ? AND period = ?",
            (user_id, period),
        ).fetchone()
        if not is_admin:
            scan_lim, coach_lim = _effective_limits(c, user_id, period)
            count = row[col]
            lim = scan_lim if kind == "scans" else coach_lim
            prev = row[alert_col] or 0
            pct = (count / lim * 100) if lim > 0 else 0
            new_level = _threshold_for(pct)
            if new_level > prev:
                for t in _THRESHOLDS:
                    if prev < t <= new_level:
                        c.execute(
                            "INSERT INTO admin_alerts "
                            "(user_id, email, plan, metric, threshold, scans, coach, period) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (user_id, email, plan, kind, t, row["scans"], row["coach"], period),
                        )
                        fired.append({
                            "user_id": user_id, "email": email, "plan": plan,
                            "metric": kind, "threshold": t,
                            "scans": row["scans"], "coach": row["coach"],
                            "period": period,
                            "date": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        })
                c.execute(
                    f"UPDATE usage SET {alert_col} = ? WHERE user_id = ? AND period = ?",
                    (new_level, user_id, period),
                )
    if fired:
        _push_alerts(fired)
    return fired


def _push_alerts(alerts: list) -> None:
    for a in alerts:
        print(
            f"[caloria][ALERT] {a['email']} reached {a['threshold']}% of "
            f"{a['metric']} cap — scans={a['scans']} coach={a['coach']} "
            f"plan={a['plan']} ({a['date']})"
        )
    if not config.ALERT_WEBHOOK_URL:
        return
    try:
        body = json.dumps({"type": "caloria_usage_alert", "alerts": alerts}).encode("utf-8")
        req = urllib.request.Request(
            config.ALERT_WEBHOOK_URL, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=5).close()
    except (urllib.error.URLError, OSError) as e:  # never let alerting break a request
        print(f"[caloria][ALERT] webhook failed: {e}")


# ---------------- admin overrides (no code changes needed) ----------------
def _uid_for_email(c, email: str):
    row = c.execute(
        "SELECT id, plan FROM users WHERE email = ?", ((email or "").strip().lower(),)
    ).fetchone()
    return row


def set_limit(email: str, *, scan_limit=None, coach_limit=None) -> dict:
    """Persistently raise/override a user's monthly allowance (applies every month)."""
    with db.cursor() as c:
        _ensure(c)
        row = _uid_for_email(c, email)
        if not row:
            raise ValueError("No user with that email.")
        uid = row["id"]
        c.execute("INSERT OR IGNORE INTO user_limits (user_id) VALUES (?)", (uid,))
        if scan_limit is not None:
            c.execute("UPDATE user_limits SET scan_limit = ? WHERE user_id = ?", (int(scan_limit), uid))
        if coach_limit is not None:
            c.execute("UPDATE user_limits SET coach_limit = ? WHERE user_id = ?", (int(coach_limit), uid))
    return snapshot(email)


def grant_bonus(email: str, *, scans: int = 0, coach: int = 0) -> dict:
    """Add a one-month temporary bonus to the user's current-period allowance."""
    period = current_period()
    with db.cursor() as c:
        _ensure(c)
        row = _uid_for_email(c, email)
        if not row:
            raise ValueError("No user with that email.")
        uid = row["id"]
        c.execute("INSERT OR IGNORE INTO usage (user_id, period) VALUES (?, ?)", (uid, period))
        c.execute(
            "UPDATE usage SET scan_bonus = scan_bonus + ?, coach_bonus = coach_bonus + ? "
            "WHERE user_id = ? AND period = ?",
            (int(scans), int(coach), uid, period),
        )
    return snapshot(email)


def reset_usage(email: str) -> dict:
    """Zero the user's current-month usage and alert state (does not touch limits)."""
    period = current_period()
    with db.cursor() as c:
        _ensure(c)
        row = _uid_for_email(c, email)
        if not row:
            raise ValueError("No user with that email.")
        uid = row["id"]
        c.execute(
            "UPDATE usage SET scans = 0, coach = 0, scan_alert = 0, coach_alert = 0 "
            "WHERE user_id = ? AND period = ?",
            (uid, period),
        )
    return snapshot(email)


# ---------------- admin read models ----------------
def snapshot(email: str) -> dict:
    """Full internal usage snapshot for one user (admin only)."""
    period = current_period()
    with db.cursor() as c:
        _ensure(c)
        row = _uid_for_email(c, email)
        if not row:
            raise ValueError("No user with that email.")
        uid = row["id"]
        scan_lim, coach_lim = _effective_limits(c, uid, period)
        u = c.execute(
            "SELECT scans, coach, scan_bonus, coach_bonus FROM usage "
            "WHERE user_id = ? AND period = ?",
            (uid, period),
        ).fetchone()
    return {
        "email": (email or "").strip().lower(),
        "plan": row["plan"],
        "period": period,
        "scans": u["scans"] if u else 0,
        "coach": u["coach"] if u else 0,
        "scan_limit": scan_lim,
        "coach_limit": coach_lim,
        "scan_bonus": (u["scan_bonus"] if u else 0),
        "coach_bonus": (u["coach_bonus"] if u else 0),
    }


def dashboard() -> dict:
    """Business-monitoring snapshot (admin only). No user ever sees this."""
    period = current_period()
    with db.cursor() as c:
        _ensure(c)
        total_users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        subscribers = c.execute(
            "SELECT COUNT(*) n FROM users WHERE plan = 'premium'"
        ).fetchone()["n"]
        rows = c.execute(
            """SELECT u.id, u.email, u.plan,
                      COALESCE(us.scans, 0) AS scans,
                      COALESCE(us.coach, 0) AS coach
               FROM users u
               LEFT JOIN usage us ON us.user_id = u.id AND us.period = ?
               ORDER BY (COALESCE(us.scans,0) + COALESCE(us.coach,0)) DESC""",
            (period,),
        ).fetchall()
        custom = {
            r["user_id"]: r
            for r in c.execute("SELECT user_id, scan_limit, coach_limit FROM user_limits").fetchall()
        }

    users = []
    total_scans = total_coach = 0
    for r in rows:
        cl = custom.get(r["id"])
        scan_lim = config.PREMIUM_SCAN_LIMIT
        coach_lim = config.PREMIUM_COACH_LIMIT
        if cl:
            if cl["scan_limit"] is not None:
                scan_lim = cl["scan_limit"]
            if cl["coach_limit"] is not None:
                coach_lim = cl["coach_limit"]
        scans, coach = r["scans"], r["coach"]
        total_scans += scans
        total_coach += coach
        scan_pct = round(scans / scan_lim * 100) if scan_lim else 0
        coach_pct = round(coach / coach_lim * 100) if coach_lim else 0
        users.append({
            "email": r["email"], "plan": r["plan"],
            "scans": scans, "coach": coach,
            "scan_limit": scan_lim, "coach_limit": coach_lim,
            "scan_pct": scan_pct, "coach_pct": coach_pct,
            "approaching": scan_pct >= 75 or coach_pct >= 75,
            "at_limit": scan_pct >= 100 or coach_pct >= 100,
        })

    active = [u for u in users if (u["scans"] + u["coach"]) > 0]
    avg_total = (
        sum(u["scans"] + u["coach"] for u in active) / len(active) if active else 0
    )
    # "Exceeding typical usage" = well above the active-user average (and not trivial).
    anomalies = [
        u for u in users
        if (u["scans"] + u["coach"]) >= max(20, 2 * avg_total) and (u["scans"] + u["coach"]) > 0
    ]

    return {
        "period": period,
        "total_users": total_users,
        "active_subscribers": subscribers,
        "active_users_this_month": len(active),
        "totals": {"scans": total_scans, "coach": total_coach},
        "avg_actions_per_active_user": round(avg_total, 1),
        "defaults": {
            "scan_limit": config.PREMIUM_SCAN_LIMIT,
            "coach_limit": config.PREMIUM_COACH_LIMIT,
        },
        "users": users,
        "approaching_limit": [u for u in users if u["approaching"]],
        "exceeding_typical": anomalies,
    }


def recent_alerts(limit: int = 100) -> list:
    with db.cursor() as c:
        _ensure(c)
        rows = c.execute(
            "SELECT created_at, email, plan, metric, threshold, scans, coach, period "
            "FROM admin_alerts ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]
