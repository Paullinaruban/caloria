"""Developer-only OpenAI usage, cost & performance monitor.

Logs ONE row per OpenAI API call (meal-scan vision, meal-scan text, coach) — both
successes and failures — using the EXACT token counts the API returns and the
real USD cost from current model pricing. Surfaced admin-only via
/api/admin/ai-usage. Logging is best-effort and never raises into the request path.
"""
import db

# Real OpenAI pricing — USD per 1,000,000 tokens. Update if OpenAI changes prices.
PRICING = {
    "gpt-4o":       {"in": 2.50, "out": 10.00},
    "gpt-4o-mini":  {"in": 0.15, "out": 0.60},
    "gpt-4.1":      {"in": 2.00, "out": 8.00},
    "gpt-4.1-mini": {"in": 0.40, "out": 1.60},
    "gpt-4.1-nano": {"in": 0.10, "out": 0.40},
    "o4-mini":      {"in": 1.10, "out": 4.40},
}
_DEFAULT = {"in": 2.50, "out": 10.00}  # unknown model → assume gpt-4o pricing


def _price(model: str) -> dict:
    m = (model or "").lower()
    best = None
    for key, p in PRICING.items():
        if m.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, p)
    return best[1] if best else _DEFAULT


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p = _price(model)
    return round(prompt_tokens / 1_000_000 * p["in"] + completion_tokens / 1_000_000 * p["out"], 6)


def _ensure():
    with db.cursor() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS ai_usage (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id           INTEGER,
                kind              TEXT,
                model             TEXT,
                prompt_tokens     INTEGER,
                completion_tokens INTEGER,
                total_tokens      INTEGER,
                cost_usd          REAL,
                duration_ms       INTEGER,
                status            TEXT DEFAULT 'ok',
                error             TEXT,
                created_at        TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # Migrate older tables that predate status/error.
        for col, decl in (("status", "TEXT DEFAULT 'ok'"), ("error", "TEXT")):
            cols = [r["name"] for r in c.execute("PRAGMA table_info(ai_usage)")]
            if col not in cols:
                c.execute(f"ALTER TABLE ai_usage ADD COLUMN {col} {decl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_user ON ai_usage(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage(created_at)")


def record(usage: dict, model: str, duration_ms: float, user_id=None, kind: str = "text") -> float:
    """Persist one successful OpenAI call. Returns the cost; never raises."""
    try:
        _ensure()
        u = usage or {}
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
        tt = int(u.get("total_tokens") or (pt + ct))
        cost = cost_usd(model, pt, ct)
        with db.cursor() as c:
            c.execute(
                "INSERT INTO ai_usage (user_id, kind, model, prompt_tokens, completion_tokens, "
                "total_tokens, cost_usd, duration_ms, status) VALUES (?,?,?,?,?,?,?,?, 'ok')",
                (user_id, kind, model, pt, ct, tt, cost, int(duration_ms)),
            )
        print(f"[caloria][AI] {kind} model={model} in={pt} out={ct} cost=${cost:.5f} {int(duration_ms)}ms user={user_id}")
        return cost
    except Exception as e:  # noqa: BLE001 — monitoring must never break a feature
        print(f"[caloria][AI] usage log failed: {e}")
        return 0.0


def record_error(kind: str, model: str, duration_ms: float, user_id=None, status: str = "error", error: str = "") -> None:
    """Persist a failed/timed-out OpenAI call (cost 0). Never raises."""
    try:
        _ensure()
        with db.cursor() as c:
            c.execute(
                "INSERT INTO ai_usage (user_id, kind, model, prompt_tokens, completion_tokens, "
                "total_tokens, cost_usd, duration_ms, status, error) VALUES (?,?,?,0,0,0,0,?,?,?)",
                (user_id, kind, model, int(duration_ms), status, str(error)[:300]),
            )
        print(f"[caloria][AI] {kind} {status.upper()} model={model} {int(duration_ms)}ms user={user_id} :: {str(error)[:80]}")
    except Exception as e:  # noqa: BLE001
        print(f"[caloria][AI] error log failed: {e}")


# ---------------- admin dashboard queries ----------------
def recent(limit: int = 100, start: str = None, end: str = None, kind: str = None) -> list:
    _ensure()
    where, args = ["1=1"], []
    if start: where.append("a.created_at >= ?"); args.append(start)
    if end:   where.append("a.created_at <= ?"); args.append(end + " 23:59:59")
    if kind and kind != "all": where.append("a.kind = ?"); args.append(kind)
    args.append(int(limit))
    with db.cursor() as c:
        rows = c.execute(
            "SELECT a.id, a.user_id, u.email, a.kind, a.model, a.prompt_tokens, a.completion_tokens, "
            "a.total_tokens, a.cost_usd, a.duration_ms, a.status, a.error, a.created_at "
            "FROM ai_usage a LEFT JOIN users u ON u.id = a.user_id "
            f"WHERE {' AND '.join(where)} ORDER BY a.id DESC LIMIT ?", args).fetchall()
    return [dict(r) for r in rows]


def analytics() -> dict:
    """Full analytics payload for the admin dashboard — all from real logged data."""
    _ensure()
    with db.cursor() as c:
        def one(sql, *a):
            return c.execute(sql, a).fetchone()
        def rows(sql, *a):
            return [dict(r) for r in c.execute(sql, a).fetchall()]

        OK = "status='ok'"  # successful (billable) calls only for cost/token stats

        # ---- spend ----
        spend = {
            "today":  one(f"SELECT COALESCE(SUM(cost_usd),0) v FROM ai_usage WHERE {OK} AND created_at >= date('now','start of day')")["v"],
            "week":   one(f"SELECT COALESCE(SUM(cost_usd),0) v FROM ai_usage WHERE {OK} AND created_at >= date('now','-7 days')")["v"],
            "month":  one(f"SELECT COALESCE(SUM(cost_usd),0) v FROM ai_usage WHERE {OK} AND created_at >= date('now','-30 days')")["v"],
            "total":  one(f"SELECT COALESCE(SUM(cost_usd),0) v FROM ai_usage WHERE {OK}")["v"],
        }
        spend = {k: round(v, 6) for k, v in spend.items()}
        spend["est_monthly"] = spend["month"]  # last-30-day spend = projected monthly bill

        # ---- performance (all rows, incl. failures) ----
        tot = one("SELECT COUNT(*) n, SUM(status='ok') ok, SUM(status='error') err, SUM(status='timeout') tmo, "
                  "COALESCE(AVG(duration_ms),0) ms FROM ai_usage")
        n = tot["n"] or 0
        perf = {
            "total": n, "ok": tot["ok"] or 0, "errors": tot["err"] or 0, "timeouts": tot["tmo"] or 0,
            "avg_ms": round(tot["ms"]),
            "success_rate": round((tot["ok"] or 0) / n * 100, 1) if n else 100.0,
            "error_rate": round((tot["err"] or 0) / n * 100, 1) if n else 0.0,
            "timeout_rate": round((tot["tmo"] or 0) / n * 100, 1) if n else 0.0,
            "avg_scan_ms": round(one(f"SELECT COALESCE(AVG(duration_ms),0) v FROM ai_usage WHERE {OK} AND kind='scan_vision'")["v"]),
            "avg_coach_ms": round(one(f"SELECT COALESCE(AVG(duration_ms),0) v FROM ai_usage WHERE {OK} AND kind='coach'")["v"]),
            "slowest": rows("SELECT a.id, a.kind, a.model, a.duration_ms, a.cost_usd, a.user_id, u.email, a.created_at "
                            "FROM ai_usage a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.duration_ms DESC LIMIT 10"),
            "recent_errors": rows("SELECT a.kind, a.model, a.status, a.error, a.user_id, a.created_at "
                                  "FROM ai_usage a WHERE a.status!='ok' ORDER BY a.id DESC LIMIT 10"),
        }

        # ---- users ----
        n_scans = one("SELECT COUNT(*) n FROM ai_usage WHERE kind='scan_vision' AND status='ok'")["n"]
        n_coach = one("SELECT COUNT(*) n FROM ai_usage WHERE kind='coach' AND status='ok'")["n"]
        active_days = one("SELECT COUNT(DISTINCT substr(created_at,1,10)) d FROM ai_usage WHERE status='ok'")["d"] or 1
        users = {
            "total_ai_users": one("SELECT COUNT(DISTINCT user_id) n FROM ai_usage WHERE user_id IS NOT NULL")["n"],
            "dau": one("SELECT COUNT(DISTINCT user_id) n FROM ai_usage WHERE created_at >= date('now','start of day')")["n"],
            "wau": one("SELECT COUNT(DISTINCT user_id) n FROM ai_usage WHERE created_at >= date('now','-7 days')")["n"],
            "mau": one("SELECT COUNT(DISTINCT user_id) n FROM ai_usage WHERE created_at >= date('now','-30 days')")["n"],
            "avg_scans_per_day": round(n_scans / active_days, 1),
            "avg_coach_per_day": round(n_coach / active_days, 1),
            "most_active": rows(
                "SELECT a.user_id, u.email, "
                "SUM(a.kind='scan_vision' AND a.status='ok') scans, "
                "SUM(a.kind='coach' AND a.status='ok') coach, "
                "COUNT(*) requests, COALESCE(SUM(a.cost_usd),0) cost "
                "FROM ai_usage a LEFT JOIN users u ON u.id=a.user_id "
                "GROUP BY a.user_id ORDER BY requests DESC LIMIT 20"),
        }

        # ---- cost analytics ----
        cost = {
            "by_model": rows(f"SELECT model, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost, "
                             f"COALESCE(SUM(total_tokens),0) tokens FROM ai_usage WHERE {OK} "
                             "GROUP BY model ORDER BY cost DESC"),
            "by_kind": rows(f"SELECT kind, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost, "
                            f"COALESCE(AVG(cost_usd),0) avg_cost, COALESCE(AVG(duration_ms),0) avg_ms "
                            f"FROM ai_usage WHERE {OK} GROUP BY kind ORDER BY cost DESC"),
            "by_day": rows(f"SELECT substr(created_at,1,10) bucket, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost "
                           f"FROM ai_usage WHERE {OK} GROUP BY bucket ORDER BY bucket DESC LIMIT 30"),
            "by_week": rows(f"SELECT strftime('%Y-W%W', created_at) bucket, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost "
                            f"FROM ai_usage WHERE {OK} GROUP BY bucket ORDER BY bucket DESC LIMIT 12"),
            "by_month": rows(f"SELECT strftime('%Y-%m', created_at) bucket, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost "
                             f"FROM ai_usage WHERE {OK} GROUP BY bucket ORDER BY bucket DESC LIMIT 12"),
            "top20": rows("SELECT a.id, a.kind, a.model, a.prompt_tokens, a.completion_tokens, a.total_tokens, "
                          "a.cost_usd, a.duration_ms, a.user_id, u.email, a.created_at "
                          f"FROM ai_usage a LEFT JOIN users u ON u.id=a.user_id WHERE a.{OK} "
                          "ORDER BY a.cost_usd DESC LIMIT 20"),
        }

        # A meal scan = 1 vision + 1 text call → cost-per-scan sums both / #scans.
        scan_cost = one("SELECT COALESCE(SUM(cost_usd),0) v FROM ai_usage "
                        "WHERE status='ok' AND kind IN ('scan_vision','scan_text')")["v"]
        coach_avg = one("SELECT COALESCE(AVG(cost_usd),0) v FROM ai_usage WHERE status='ok' AND kind='coach'")["v"]

    return {
        "spend": spend,
        "performance": perf,
        "users": users,
        "cost": cost,
        "totals": {
            "requests": perf["total"],
            "scans": n_scans,
            "coach": n_coach,
            "avg_cost_per_scan": round(scan_cost / n_scans, 6) if n_scans else 0.0,
            "avg_cost_per_coach": round(coach_avg, 6),
        },
    }
