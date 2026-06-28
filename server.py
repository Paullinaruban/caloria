"""Caloria API server (Python stdlib only).

Auth:     POST /api/auth/signup | /api/auth/login | /api/auth/logout
          GET  /api/me   POST /api/onboarding
Scanning: POST /api/analyze (gated)   POST /api/correct
History:  GET  /api/meals   POST /api/meals   DELETE /api/meals?id=
Planner:  POST /api/mealplan   POST /api/mealplan/meal   POST /api/mealimage
Billing:  POST /api/billing/checkout   POST /api/billing/webhook
Meta:     GET  /api/health | /api/config

Run:  python3 backend/server.py
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import account
import admin
import aicost
import auth
import insights
import journey
import mailer
import notifications
import ritual
import threading
import billing
import coach
import community
import config
import db
import images
import learning
import mealplan
import pipeline
import ratelimit
import turnstile
import usage
import vision
import workout

MAX_BODY = 16 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "Caloria/2.0"

    # ---- io helpers ----
    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", config.ALLOWED_ORIGIN)
        if config.ALLOWED_ORIGIN != "*":
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _client_ip(self) -> str:
        """Caller IP — trusts X-Forwarded-For only when explicitly behind a proxy."""
        if config.TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "?"

    def _rate_limited(self, bucket: str, ident: str) -> bool:
        """Return True (and send 429) if this (bucket, ident) is over the limit."""
        ok, retry = ratelimit.check(bucket, ident)
        if not ok:
            self.send_response(429)
            self.send_header("Retry-After", str(retry))
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(json.dumps({
                "error": "Too many attempts. Please wait a moment and try again.",
            }).encode())
            return True
        return False

    def _raw_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY:
            return b""
        return self.rfile.read(length)

    def _json_body(self):
        raw = self._raw_body()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _user(self):
        """Return the authenticated user row, or None."""
        h = self.headers.get("Authorization", "")
        token = h[7:].strip() if h.lower().startswith("bearer ") else None
        return auth.user_for_token(token)

    def _require_user(self):
        u = self._user()
        if not u:
            self._send(401, {"error": "Please sign in.", "auth": True})
            return None
        return u

    def _is_admin(self, u) -> bool:
        return bool(u) and (u["email"] or "").lower() in config.ADMIN_EMAILS

    def _require_admin(self):
        """Admin-only gate. Returns the user row or None (and 403s) — never leaks."""
        u = self._user()
        if not u or not self._is_admin(u):
            self._send(403, {"error": "forbidden"})
            return None
        return u

    def _require_premium(self, u) -> bool:
        """Hard paywall: only active paid subscribers may use this. Returns True if OK."""
        if auth.is_premium(u):
            return True
        self._send(402, {
            "error": "A Caloria subscription is required to use this feature.",
            "upgrade": True,
        })
        return False

    def _require_verified(self, u) -> bool:
        """Block unverified accounts from cost-bearing features. Returns True if OK."""
        if not config.REQUIRE_EMAIL_VERIFICATION or auth.is_verified(u):
            return True
        self._send(403, {
            "error": "Please verify your email to unlock this feature. "
                     "Check your inbox for the confirmation link.",
            "needs_verification": True,
        })
        return False

    def log_message(self, fmt, *args):
        print(f"[caloria] {self.address_string()} {fmt % args}")

    # ---- method dispatch ----
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._send(200, {"ok": True})
        if path == "/api/config":
            return self._send(200, {
                "openai_configured": config.openai_ready(),
                "usda_key": "DEMO_KEY" if config.USDA_API_KEY == "DEMO_KEY" else "configured",
                "stripe_configured": config.stripe_ready(),
                "images_enabled": images.available(),
                "free_scan_limit": config.FREE_SCAN_LIMIT,
                "price_monthly": config.PRICE_MONTHLY_DISPLAY,
                "price_yearly": config.PRICE_YEARLY_DISPLAY,
                "trial_days": config.TRIAL_DAYS,
                # Public bits the frontend needs (site key is meant to be public).
                "turnstile_site_key": config.TURNSTILE_SITE_KEY,
                "require_verification": config.REQUIRE_EMAIL_VERIFICATION,
            })
        if path == "/api/me":
            u = self._require_user()
            if u:
                self._send(200, {"user": auth.public_user(u)})
            return
        if path == "/api/meals":
            return self._list_meals()
        if path == "/api/journey":
            u = self._require_user()
            if u:
                self._send(200, journey.compute(u["id"]))
            return
        if path == "/api/craving":
            u = self._require_user()
            if u:
                qs = parse_qs(urlparse(self.path).query)
                food = (qs.get("food") or [""])[0]
                self._send(200, insights.craving_insight(u["id"], food))
            return
        if path == "/api/ritual":
            u = self._require_user()
            if u:
                self._send(200, ritual.state(u["id"]))
            return
        if path == "/api/workout/active":
            self._workout_active()
            return
        if path == "/api/workout/history":
            self._workout_history()
            return
        if path == "/api/notifications":
            u = self._require_user()
            if u:
                items, has_new, _keys = notifications.generate(u["id"])
                if not items:
                    items = [notifications.future_you_empty()]
                self._send(200, {"notifications": items, "has_new": has_new})
            return
        if path == "/api/billing/status":
            u = self._require_user()
            if u:
                self._send(200, billing.subscription_info(u))
            return
        if path == "/api/coach/history":
            u = self._require_user()
            if u:
                self._send(200, {"messages": coach.history(u["id"]) if auth.is_premium(u) else []})
            return
        # ---- admin: owner-only business monitoring (never exposed to users) ----
        if path == "/api/admin/dashboard":
            if not self._require_admin():
                return
            return self._send(200, usage.dashboard())
        if path == "/api/admin/alerts":
            if not self._require_admin():
                return
            return self._send(200, {"alerts": usage.recent_alerts()})
        if path == "/api/admin/overview":
            if not self._require_admin():
                return
            return self._send(200, admin.business_overview())
        if path == "/api/admin/analytics":
            if not self._require_admin():
                return
            return self._send(200, admin.analytics())
        if path == "/api/admin/ai-usage":
            if not self._require_admin():
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                limit = min(1000, max(1, int((qs.get("limit") or ["200"])[0])))
            except (TypeError, ValueError):
                limit = 200
            start = (qs.get("start") or [None])[0]
            end = (qs.get("end") or [None])[0]
            kind = (qs.get("kind") or [None])[0]
            return self._send(200, {
                "analytics": aicost.analytics(),
                "recent": aicost.recent(limit, start=start, end=end, kind=kind),
            })
        if path == "/api/admin/subscriptions":
            if not self._require_admin():
                return
            return self._send(200, admin.subscriptions())
        if path == "/api/admin/users":
            if not self._require_admin():
                return
            qs = parse_qs(urlparse(self.path).query)
            return self._send(200, {"users": admin.search_users((qs.get("q") or [""])[0])})
        if path == "/api/admin/user":
            if not self._require_admin():
                return
            qs = parse_qs(urlparse(self.path).query)
            email = (qs.get("email") or [""])[0]
            try:
                return self._send(200, admin.user_detail(email))
            except ValueError as e:
                return self._send(404, {"error": str(e)})
        # ---- community (Supermodel Wellness Club) ----
        if path == "/api/community/stats":
            return self._send(200, community.stats())  # public — powers social proof
        if path == "/api/community/feed":
            u = self._require_user()
            if not u:
                return
            community.ensure_profile(u)
            qs = parse_qs(urlparse(self.path).query)
            ptype = (qs.get("type") or [None])[0]
            author = (qs.get("author") or [None])[0]
            return self._send(200, {"posts": community.feed(u["id"], ptype=ptype, author_id=int(author) if author else None)})
        if path == "/api/community/wall":
            u = self._require_user()
            if not u:
                return
            community.ensure_profile(u)
            return self._send(200, {"posts": community.wall(u["id"])})
        if path == "/api/community/profile":
            u = self._require_user()
            if not u:
                return
            community.ensure_profile(u)
            qs = parse_qs(urlparse(self.path).query)
            target = (qs.get("user_id") or [str(u["id"])])[0]
            prof = community.get_profile(int(target), u["id"])
            return self._send(200, {"profile": prof, "posts": community.feed(u["id"], author_id=int(target))})
        if path == "/api/community/comments":
            u = self._require_user()
            if not u:
                return
            qs = parse_qs(urlparse(self.path).query)
            pid = (qs.get("post_id") or [None])[0]
            return self._send(200, {"comments": community.comments(int(pid)) if pid else []})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path == "/api/meals":
            return self._delete_meal()
        if path == "/api/me":
            return self._delete_account()
        return self._send(404, {"error": "not found"})

    def _delete_account(self):
        u = self._require_user()
        if not u:
            return
        try:
            account.delete_account(u["id"])
        except Exception as e:  # noqa: BLE001
            print(f"[caloria] account deletion failed for user {u['id']}: {e}")
            return self._send(500, {"error": "We couldn't delete your account. Please try again or contact support."})
        self._send(200, {"ok": True, "deleted": True})

    def do_POST(self):
        path = urlparse(self.path).path
        # Webhook needs the raw body for signature verification.
        if path == "/api/billing/webhook":
            return self._webhook()
        try:
            data = self._json_body()
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "invalid JSON body"})

        routes = {
            "/api/auth/signup": self._signup,
            "/api/auth/login": self._login,
            "/api/auth/logout": self._logout,
            "/api/auth/verify-code": self._verify_code,
            "/api/auth/resend": self._resend_verification,
            "/api/auth/forgot": self._forgot_password,
            "/api/auth/reset": self._reset_password,
            "/api/onboarding": self._onboarding,
            "/api/analyze": self._analyze,
            "/api/correct": self._correct,
            "/api/meals": self._save_meal,
            "/api/mealplan": self._mealplan,
            "/api/mealplan/meal": self._mealplan_meal,
            "/api/mealimage": self._mealimage,
            "/api/workout": self._workout,
            "/api/coach": self._coach,
            "/api/community/post": self._community_post,
            "/api/community/like": self._community_like,
            "/api/community/comment": self._community_comment,
            "/api/community/follow": self._community_follow,
            "/api/community/profile": self._community_save_profile,
            "/api/ritual/checkin": self._ritual_checkin,
            "/api/ritual/reflect": self._ritual_reflect,
            "/api/ritual/freeze": self._ritual_freeze,
            "/api/notifications/seen": self._notifications_seen,
            "/api/workout/complete": self._workout_complete,
            "/api/billing/checkout": self._checkout,
            "/api/billing/portal": self._billing_portal,
            "/api/admin/override": self._admin_override,
            "/api/admin/user/action": self._admin_user_action,
            "/api/admin/email-test": self._admin_email_test,
        }
        handler = routes.get(path)
        if handler:
            return handler(data)
        return self._send(404, {"error": "not found"})

    # ---- auth ----
    def _signup(self, data):
        ip = self._client_ip()
        if self._rate_limited("signup", ip):
            return
        if not turnstile.verify(data.get("captcha_token", ""), ip):
            return self._send(400, {"error": "Bot check failed. Please try again."})
        try:
            self._send(200, auth.signup(data.get("email"), data.get("password"), data.get("name", "")))
        except auth.AuthError as e:
            self._send(e.code, {"error": e.message})

    def _login(self, data):
        ip = self._client_ip()
        email = str(data.get("email", "")).strip().lower()
        # Per-IP and per-account limits blunt brute-force / credential stuffing.
        if self._rate_limited("login", ip) or (email and self._rate_limited("login_email", email)):
            return
        if not turnstile.verify(data.get("captcha_token", ""), ip):
            return self._send(400, {"error": "Bot check failed. Please try again."})
        try:
            self._send(200, auth.login(data.get("email"), data.get("password")))
        except auth.AuthError as e:
            self._send(e.code, {"error": e.message})

    def _logout(self, data):
        h = self.headers.get("Authorization", "")
        if h.lower().startswith("bearer "):
            auth.logout(h[7:].strip())
        self._send(200, {"ok": True})

    # ---- email verification (6-digit code) & password reset ----
    def _verify_code(self, data):
        # Brute-force guard (per IP) on top of the per-code attempt cap.
        if self._rate_limited("verify_code", self._client_ip()):
            return
        email = str(data.get("email", ""))
        code = str(data.get("code", ""))
        if not auth.verify_email_code(email, code):
            return self._send(400, {
                "error": "That code is incorrect or has expired. "
                         "Please check the code or request a new one.",
            })
        self._send(200, {"ok": True, "verified": True})

    def _resend_verification(self, data):
        if self._rate_limited("resend", self._client_ip()):
            return
        # Prefer the signed-in user; fall back to a supplied email. Always neutral.
        u = self._user()
        email = u["email"] if u else str(data.get("email", ""))
        auth.resend_verification(email)
        self._send(200, {"ok": True})

    def _forgot_password(self, data):
        if self._rate_limited("forgot", self._client_ip()):
            return
        auth.request_reset(str(data.get("email", "")))
        # Always 200 — never reveal whether the email exists (no enumeration).
        self._send(200, {"ok": True})

    def _reset_password(self, data):
        try:
            ok = auth.reset_password(str(data.get("token", "")), str(data.get("password", "")))
        except auth.AuthError as e:
            return self._send(e.code, {"error": e.message})
        if not ok:
            return self._send(400, {
                "error": "This reset link is invalid or has expired. Please request a new one.",
                "expired": True,
            })
        self._send(200, {"ok": True})

    def _notifications_seen(self, data):
        u = self._require_user()
        if not u:
            return
        keys = data.get("keys") or []
        if isinstance(keys, list):
            notifications.mark_seen(u["id"], [str(k) for k in keys if k])
        self._send(200, {"ok": True})

    def _workout_complete(self, data):
        u = self._require_user()
        if not u:
            return
        if not self._require_premium(u):   # workouts are a paid feature
            return
        self._send(200, workout.complete(u["id"]))

    # ---- daily ritual ----
    def _ritual_checkin(self, data):
        u = self._require_user()
        if not u:
            return
        try:
            res = ritual.save_checkin(u["id"], data.get("energy"), data.get("mood"),
                                      data.get("sleep"), data.get("hydration"))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        self._send(200, res)

    def _ritual_reflect(self, data):
        u = self._require_user()
        if not u:
            return
        try:
            res = ritual.save_reflection(u["id"], data.get("reflection"))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        self._send(200, res)

    def _ritual_freeze(self, data):
        u = self._require_user()
        if not u:
            return
        try:
            res = ritual.use_freeze(u["id"])
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        self._send(200, res)

    def _onboarding(self, data):
        u = self._require_user()
        if not u:
            return
        profile = data.get("profile") or data
        self._send(200, auth.save_profile(u["id"], profile))

    # ---- scanning (gated) ----
    def _analyze(self, data):
        u = self._require_user()
        if not u:
            return
        if not self._require_verified(u):
            return
        if not self._require_premium(u):   # no free scans — paid subscription required
            return
        admin = self._is_admin(u)
        # Premium monthly safeguard — internal only. Neutral message, no mention of
        # limits/quotas, no upgrade prompt (they already pay).
        if not usage.allowed(u["id"], "scans", is_admin=admin):
            return self._send(503, {
                "error": "We’re processing a high volume of requests right now. "
                         "Please try again in a little while."
            })
        image = data.get("image", "")
        if not isinstance(image, str) or not image.startswith("data:image"):
            return self._send(400, {"error": "expected { image: <data URL> }"})
        if not config.openai_ready():
            # AI vision not configured — fail cleanly and never leak provider details.
            return self._send(503, {
                "error": "AI meal scanning is temporarily unavailable. Please try again soon."
            })
        try:
            result = pipeline.analyze_meal(image, user_id=u["id"])
        except vision.VisionError as e:
            print(f"[caloria] vision error for user {u['id']}: {e}")  # detail stays server-side
            return self._send(503, {
                "error": "We couldn't analyse that photo right now. Please try again."
            })
        except Exception as e:  # noqa: BLE001
            # Log full detail server-side; return a generic message (no internals).
            print(f"[caloria] analyze failed for user {u['id']}: {e}")
            return self._send(500, {"error": "Something went wrong analysing that photo. Please try again."})

        with db.cursor() as c:
            c.execute("UPDATE users SET scans_used = scans_used + 1 WHERE id = ?", (u["id"],))
        # Meter only on success (failed AI calls cost nothing → don't count them).
        usage.record(u["id"], "scans", email=u["email"], plan=u["plan"], is_admin=admin)
        result["scans_used"] = u["scans_used"] + 1
        self._send(200, result)

    def _correct(self, data):
        if not self._require_user():
            return
        saved = learning.record_corrections(data.get("corrections", []))
        self._send(200, {"ok": True, "saved": saved})

    # ---- history ----
    def _list_meals(self):
        u = self._require_user()
        if not u:
            return
        with db.cursor() as c:
            rows = c.execute(
                "SELECT id, name, image, data_json, created_at FROM meals "
                "WHERE user_id = ? ORDER BY id DESC LIMIT 200",
                (u["id"],),
            ).fetchall()
        meals = [{
            "id": r["id"], "name": r["name"], "image": r["image"],
            "created_at": r["created_at"], **json.loads(r["data_json"]),
        } for r in rows]
        self._send(200, {"meals": meals})

    def _save_meal(self, data):
        u = self._require_user()
        if not u:
            return
        meal = data.get("meal") or {}
        payload = {k: meal.get(k) for k in
                   ("calories", "protein", "carbs", "fats", "fiber", "sugar", "sodium")}
        with db.cursor() as c:
            c.execute(
                "INSERT INTO meals (user_id, name, image, data_json) VALUES (?,?,?,?)",
                (u["id"], str(meal.get("name", "Meal"))[:120], meal.get("image"), json.dumps(payload)),
            )
            meal_id = c.lastrowid
        self._send(200, {"ok": True, "id": meal_id})

    def _delete_meal(self):
        u = self._require_user()
        if not u:
            return
        qs = parse_qs(urlparse(self.path).query)
        mid = (qs.get("id") or [None])[0]
        if not mid:
            return self._send(400, {"error": "missing id"})
        with db.cursor() as c:
            c.execute("DELETE FROM meals WHERE id = ? AND user_id = ?", (int(mid), u["id"]))
        self._send(200, {"ok": True})

    # ---- meal planner ----
    def _mealplan(self, data):
        u = self._require_user()
        if not u:
            return
        if not self._require_premium(u):   # paid-only feature
            return
        if not u["targets_json"]:
            return self._send(400, {"error": "Complete onboarding first.", "needs_onboarding": True})
        targets = json.loads(u["targets_json"])
        modes = data.get("modes") or []
        basic = False
        # Honour allergies / food exclusions captured during onboarding.
        exclusions = list(data.get("exclusions") or [])
        if u["profile_json"]:
            prof = json.loads(u["profile_json"])
            for key in ("allergies", "exclusions"):
                v = prof.get(key)
                if isinstance(v, list):
                    exclusions += v
                elif isinstance(v, str) and v.strip():
                    exclusions += [x for x in v.replace(";", ",").split(",")]
        # Per-user rotating offset so each generation surfaces different meals.
        ck = f"mealcursor:{u['id']}"
        try:
            offset = int(db.kv_get(ck) or 0)
        except (TypeError, ValueError):
            offset = 0
        db.kv_set(ck, str(offset + 1))
        try:
            plan = mealplan.generate_plan(targets, modes, exclusions=exclusions, offset=offset)
        except Exception as e:  # noqa: BLE001
            return self._send(503, {"error": str(e)})
        plan["basic"] = basic
        self._send(200, plan)

    def _mealplan_meal(self, data):
        u = self._require_user()
        if not u:
            return
        if not auth.is_premium(u):
            return self._send(402, {
                "error": "Swapping & regenerating meals is a Premium feature.",
                "upgrade": True,
            })
        try:
            meal = mealplan.regenerate_meal(
                str(data.get("slot", "Meal")),
                int(data.get("target_calories", 400)),
                data.get("modes") or [],
                str(data.get("avoid", "")),
            )
        except Exception as e:  # noqa: BLE001
            return self._send(503, {"error": str(e)})
        self._send(200, {"meal": meal})

    def _mealimage(self, data):
        u = self._require_user()
        if not u:
            return
        if not self._require_premium(u):   # paid-only (can incur image-gen cost)
            return
        try:
            url = images.generate(str(data.get("dish", "")).strip()[:120])
        except images.ImageError as e:
            return self._send(503, {"error": str(e)})
        self._send(200, {"url": url})

    # ---- workout generator ----
    def _workout(self, data):
        u = self._require_user()
        if not u:
            return
        if not self._require_premium(u):   # paid-only feature
            return
        category = (data.get("category") or "full_body").strip()
        goal = (data.get("goal") or "").strip()
        equipment = (data.get("equipment") or "").strip()
        level = (data.get("level") or "").strip()
        try:
            duration = int(data.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        # Fall back to the saved onboarding profile, then to sensible defaults.
        base = json.loads(u["profile_json"]) if u["profile_json"] else {}
        goal = goal or base.get("goal") or "fat_loss"
        level = level or base.get("level") or "intermediate"
        equipment = equipment or base.get("equipment") or "gym"
        duration = duration or int(base.get("duration") or 30)
        try:
            plan = workout.generate_one(u["id"], category, goal=goal,
                                        equipment=equipment, duration=duration, level=level)
        except Exception as e:  # noqa: BLE001
            return self._send(503, {"error": str(e)})
        self._send(200, {"plan": plan})

    def _workout_active(self):
        u = self._require_user()
        if not u:
            return
        if not self._require_premium(u):
            return
        self._send(200, {"plan": workout.active(u["id"])})

    def _workout_history(self):
        u = self._require_user()
        if not u:
            return
        if not self._require_premium(u):
            return
        self._send(200, {"history": workout.history(u["id"])})

    # ---- AI coach chat (premium) ----
    def _coach(self, data):
        u = self._require_user()
        if not u:
            return
        if not self._require_verified(u):
            return
        if not auth.is_premium(u):
            return self._send(402, {
                "error": "AI Coach Chat is a Premium feature. Upgrade for unlimited coaching.",
                "upgrade": True,
            })
        admin = self._is_admin(u)
        # Premium monthly safeguard — internal only. Neutral message.
        if not usage.allowed(u["id"], "coach", is_admin=admin):
            return self._send(503, {
                "error": "This feature is temporarily unavailable. Please try again shortly."
            })
        profile = json.loads(u["profile_json"]) if u["profile_json"] else None
        try:
            answer = coach.reply(u["id"], data.get("message", ""), profile)
        except Exception as e:  # noqa: BLE001
            return self._send(503, {"error": str(e)})
        usage.record(u["id"], "coach", email=u["email"], plan=u["plan"], is_admin=admin)
        self._send(200, {"reply": answer})

    # ---- admin overrides (owner-only; no code changes needed at runtime) ----
    def _admin_override(self, data):
        if not self._require_admin():
            return
        action = str(data.get("action", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        try:
            if action == "set_limit":
                res = usage.set_limit(
                    email,
                    scan_limit=data.get("scan_limit"),
                    coach_limit=data.get("coach_limit"),
                )
            elif action == "grant_bonus":
                res = usage.grant_bonus(
                    email,
                    scans=int(data.get("scans", 0) or 0),
                    coach=int(data.get("coach", 0) or 0),
                )
            elif action == "reset_usage":
                res = usage.reset_usage(email)
            elif action == "snapshot":
                res = usage.snapshot(email)
            else:
                return self._send(400, {"error": "unknown action"})
        except ValueError as e:
            return self._send(404, {"error": str(e)})
        self._send(200, {"ok": True, "user": res})

    def _admin_user_action(self, data):
        if not self._require_admin():
            return
        action = str(data.get("action", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        try:
            if action == "activate":
                res = admin.set_active(email, True)
            elif action == "deactivate":
                res = admin.set_active(email, False)
            elif action == "grant_premium":
                res = admin.set_premium(email, True)
            elif action == "revoke_premium":
                res = admin.set_premium(email, False)
            elif action == "delete":
                account.delete_by_email(email)
                return self._send(200, {"ok": True, "deleted": True})
            else:
                return self._send(400, {"error": "unknown action"})
        except ValueError as e:
            return self._send(404, {"error": str(e)})
        self._send(200, {"ok": True, "user": res})

    def _admin_email_test(self, data):
        """Send a single retention email to an account (admin only) to verify delivery."""
        if not self._require_admin():
            return
        email = str(data.get("email", "")).strip().lower()
        kind = str(data.get("type", "")).strip()
        with db.cursor() as c:
            row = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            return self._send(404, {"error": "No user with that email."})
        try:
            res = mailer.send_one(row["id"], kind, force=True)
        except Exception as e:  # noqa: BLE001
            return self._send(503, {"error": f"Email send failed: {e}"})
        self._send(200, res)

    # ---- community (Supermodel Wellness Club) ----
    def _community_post(self, data):
        u = self._require_user()
        if not u:
            return
        community.ensure_profile(u)
        pid = community.create_post(
            u["id"], str(data.get("type", "win")), data.get("text", ""),
            data.get("image"), data.get("image2"),
        )
        self._send(200, {"ok": True, "id": pid})

    def _community_like(self, data):
        u = self._require_user()
        if not u:
            return
        self._send(200, community.toggle_like(u["id"], int(data.get("post_id", 0))))

    def _community_comment(self, data):
        u = self._require_user()
        if not u:
            return
        community.ensure_profile(u)
        community.add_comment(u["id"], int(data.get("post_id", 0)), data.get("text", ""))
        self._send(200, {"ok": True, "comments": community.comments(int(data.get("post_id", 0)))})

    def _community_follow(self, data):
        u = self._require_user()
        if not u:
            return
        self._send(200, community.toggle_follow(u["id"], int(data.get("user_id", 0))))

    def _community_save_profile(self, data):
        u = self._require_user()
        if not u:
            return
        community.ensure_profile(u)
        self._send(200, {"profile": community.update_profile(
            u["id"], data.get("username"), data.get("bio"), data.get("avatar"), data.get("avatar_img"))})

    # ---- billing ----
    def _checkout(self, data):
        u = self._require_user()
        if not u:
            return
        if not self._require_verified(u):
            return
        try:
            url = billing.create_checkout(auth.public_user(u), data.get("interval", "monthly"))
        except billing.BillingError as e:
            return self._send(503, {"error": str(e)})
        self._send(200, {"url": url})

    def _billing_portal(self, data):
        u = self._require_user()
        if not u:
            return
        try:
            url = billing.create_portal(u)
        except billing.BillingError as e:
            return self._send(503, {"error": str(e)})
        self._send(200, {"url": url})

    def _webhook(self):
        raw = self._raw_body()
        sig = self.headers.get("Stripe-Signature", "")
        try:
            event = billing.verify_and_parse(raw, sig)
            billing.handle_event(event)
        except billing.BillingError as e:
            return self._send(400, {"error": str(e)})
        self._send(200, {"received": True})


def main():
    db.init_db()
    usage.init()
    # Retention-email scheduler (daemon). Only auto-sends when EMAIL_RETENTION_ENABLED.
    threading.Thread(target=mailer.run_scheduler, daemon=True).start()
    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(f"Caloria API on http://{config.HOST}:{config.PORT}")
    print(f"  OpenAI: {'configured' if config.openai_ready() else 'NOT configured'} "
          f"(vision: {config.OPENAI_VISION_MODEL}, text: {config.OPENAI_TEXT_MODEL})")
    print(f"  USDA:   {'DEMO_KEY (rate-limited)' if config.USDA_API_KEY == 'DEMO_KEY' else 'configured'}")
    print(f"  Stripe: {'configured' if config.stripe_ready() else 'NOT configured'}")
    print(f"  Email:  {'Resend configured' if config.email_ready() else 'NOT configured (links logged to console)'}")
    print(f"  Turnstile: {'on' if turnstile.enabled() else 'off (rate-limit only)'}")
    print(f"  Verify required: {config.REQUIRE_EMAIL_VERIFICATION}  |  DEV_UNLIMITED: {config.DEV_UNLIMITED}")
    print(f"  CORS origin: {config.ALLOWED_ORIGIN}")
    print(f"  Meal images: {'on' if images.available() else 'off'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
