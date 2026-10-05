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
import appauth
import aicost
import auth
import insights
import journey
import mailer
import notifications
import ritual
import threading
import billing
import club
import coach
import community
import config
import db
import images
import imagevalid
import learning
import mealplan
import pipeline
import ratelimit
import revenuecat
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
        allow = config.cors_origin_for(self.headers.get("Origin", ""))
        self.send_header("Access-Control-Allow-Origin", allow)
        if allow != "*":
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        # Hardening headers on every API response (JSON API — no framing, no sniffing).
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _send_html(self, code, html):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

    def _email_throttled(self, action: str, email: str) -> bool:
        """Per-recipient email gate (cooldown + hourly cap). Returns True (and sends
        a clear 429 with Retry-After) when the submitted address has been emailed
        too recently/often. Keyed on the submitted email only, so it reveals nothing
        about whether an account exists."""
        ok, retry = ratelimit.email_gate(action, email)
        if ok:
            return False
        self.send_response(429)
        self.send_header("Retry-After", str(retry))
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps({
            "error": f"Please wait {retry} seconds before requesting another code.",
            "retry_after": retry,
        }).encode())
        return True

    def _raw_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_BODY:
            return b""
        return self.rfile.read(length)

    def _json_body(self):
        raw = self._raw_body()
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        # Every handler treats the body as a JSON object; a non-dict (list, string,
        # number, null) would raise AttributeError on .get() further down. Reject it
        # here so do_POST turns it into a clean 400 instead of a dropped connection.
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

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

    # Endpoints reachable WITHOUT an active subscription. Everything else requires
    # active Premium (enforced centrally in every dispatcher below). This list is
    # exactly: signup, login, logout, email verification, password reset, Stripe
    # checkout/webhook/portal — plus the read-only infra the app needs to render
    # the subscription screen itself (health, config, the user's own /api/me and
    # billing status).
    _PUBLIC_PATHS = frozenset({
        "/api/health", "/api/config", "/api/me",
        "/api/auth/signup", "/api/auth/login", "/api/auth/logout",
        "/api/auth/verify-code", "/api/auth/resend", "/api/auth/change-email",
        "/api/auth/forgot", "/api/auth/reset", "/api/auth/reset-check",
        "/api/billing/checkout", "/api/billing/portal",
        "/api/billing/webhook", "/api/billing/status",
        # In-App Purchase (RevenueCat): the webhook is unauthenticated (verified
        # by a shared Authorization secret); /api/iap/sync is reachable by an
        # authenticated-but-not-yet-premium user (it's what MAKES them premium),
        # so it must bypass the premium gate — the handler checks the user itself.
        "/api/revenuecat/webhook", "/api/iap/sync",
        # Caloria Club (founding members) — public by design: visitors join with
        # just an email, before they have any account. Rate-limited + deduped.
        "/api/club/join", "/api/club/answers", "/api/club/status", "/api/club/stats",
        "/api/club/unsubscribe",
        # Onboarding reorder: the questionnaire now runs BEFORE account creation, so
        # the results screen computes targets with NO account/login. This endpoint
        # only computes (same nutrition_engine math) and never saves. The answers
        # are attached to the account afterward via the authenticated /api/onboarding.
        "/api/onboarding/preview",
        # NATIVE iOS onboarding (verify-first). App-gated inside the handlers by the
        # X-Caloria-App secret; no Turnstile. The website flow is untouched.
        "/api/app/verify/start", "/api/app/verify/check", "/api/app/register",
        "/api/app/iap/grant",
    })

    # Signed-in pre-paywall funnel: reachable by an AUTHENTICATED user who has not
    # yet subscribed. This is the onboarding step (saves the questionnaire profile
    # and computes calorie/macro targets so we can show the personalized results +
    # conversion screen BEFORE asking for payment). It unlocks NO premium feature —
    # every real product endpoint below still requires an active subscription.
    _FUNNEL_PATHS = frozenset({
        "/api/onboarding",
    })

    def _subscription_gate(self, path) -> bool:
        """Central enforcement: every non-public endpoint requires an authenticated
        user with an ACTIVE Premium subscription. Returns True to proceed; otherwise
        sends 401 (not signed in) or 402 (subscription required) and returns False."""
        if path in self._PUBLIC_PATHS:
            return True
        u = self._user()
        if not u:
            self._send(401, {"error": "Please sign in.", "auth": True})
            return False
        # MANDATORY email verification — enforced centrally, BEFORE the onboarding
        # funnel or any premium feature. An unverified account can only reach the
        # handful of _PUBLIC_PATHS above (its own /api/me, verify-code, resend,
        # change-email, logout). There is no app route it can touch until
        # email_verified == true, so verification cannot be bypassed from any client.
        if config.REQUIRE_EMAIL_VERIFICATION and not auth.is_verified(u):
            self._send(403, {
                "error": "Please verify your email to continue.",
                "needs_verification": True,
            })
            return False
        # Onboarding funnel: authentication is enough (no premium yet). Real premium
        # features are NOT in this set and remain gated below.
        if path in self._FUNNEL_PATHS:
            return True
        if not auth.is_premium(u):
            self._send(402, {
                "error": "An active Caloria subscription is required.",
                "subscription_required": True, "upgrade": True,
            })
            return False
        return True

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
        if not self._subscription_gate(path):
            return
        if path == "/api/health":
            return self._send(200, {"ok": True})
        if path == "/api/config":
            return self._send(200, {
                "openai_configured": config.openai_ready(),
                "email_configured": config.email_ready(),
                "usda_key": "DEMO_KEY" if config.USDA_API_KEY == "DEMO_KEY" else "configured",
                "stripe_configured": config.stripe_ready(),
                "images_enabled": images.available(),
                "free_scan_limit": config.FREE_SCAN_LIMIT,
                # Product price shown on the marketing page. At Checkout, Stripe
                # presents each buyer their own local currency: exact
                # currency_options amounts for US/EU/UK, Adaptive Pricing converts
                # the base for everywhere else. THB is only the internal settlement
                # currency and is never shown to US/EU/UK customers. All of these
                # derive from config.MONTHLY_PRICE_USD (single source of truth).
                "price_monthly": config.PRICE_MONTHLY_DISPLAY,
                "price_yearly": config.PRICE_YEARLY_DISPLAY,
                # Struck-through "was" anchor + numeric prices so the frontend can
                # render the anchor and compute the yearly savings % dynamically.
                "price_monthly_compare": config.PRICE_MONTHLY_COMPARE_DISPLAY,
                "price_monthly_usd": config.MONTHLY_PRICE_USD,
                "price_yearly_usd": config.YEARLY_PRICE_USD,
                "price_monthly_compare_usd": config.MONTHLY_COMPARE_USD,
                "trial_days": config.TRIAL_DAYS,
                # Public bits the frontend needs (site key is meant to be public).
                "turnstile_site_key": config.TURNSTILE_SITE_KEY,
                "require_verification": config.REQUIRE_EMAIL_VERIFICATION,
                # In-App Purchase: RevenueCat PUBLIC SDK keys (safe to expose) +
                # whether the server can verify purchases. The native app uses the
                # key matching its platform; the website ignores all of this.
                "revenuecat_apple_key": config.REVENUECAT_APPLE_KEY,
                "revenuecat_google_key": config.REVENUECAT_GOOGLE_KEY,
                "revenuecat_entitlement": config.REVENUECAT_ENTITLEMENT,
                "iap_enabled": config.revenuecat_ready(),
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
        if path == "/api/admin/community":
            if not self._require_admin():
                return
            return self._send(200, admin.community_moderation())
        # ---- Caloria Club (founding members) ----
        if path == "/api/club/stats":
            return self._send(200, club.stats())      # public — powers social proof
        if path == "/api/club/unsubscribe":
            # One-click unsubscribe from email footers — returns a branded page.
            qs = parse_qs(urlparse(self.path).query)
            ok = club.unsubscribe((qs.get("e") or [""])[0], (qs.get("t") or [""])[0])
            page = club.UNSUBSCRIBE_PAGE.replace(
                "%HEADING%", "You’ve been unsubscribed. 🤍" if ok else "This link didn’t work."
            ).replace(
                "%BODY%",
                "You won’t receive founder updates anymore. Your Founding Member place "
                "is still yours — you can come back any time." if ok else
                "Please use the unsubscribe link from the bottom of a Caloria email.",
            )
            return self._send_html(200 if ok else 400, page)
        if path == "/api/club/status":
            qs = parse_qs(urlparse(self.path).query)
            st = club.status_by_code((qs.get("code") or [""])[0])
            if not st:
                return self._send(404, {"error": "We couldn't find that membership."})
            return self._send(200, st)
        if path == "/api/club/admin/overview":
            if not self._require_admin():
                return
            return self._send(200, club.overview())
        if path == "/api/club/admin/members":
            if not self._require_admin():
                return
            qs = parse_qs(urlparse(self.path).query)

            def _int(key, default=0):
                try:
                    return int((qs.get(key) or [default])[0])
                except (TypeError, ValueError):
                    return default
            return self._send(200, {"members": club.members(
                (qs.get("q") or [""])[0],
                _int("limit", 200),
                since_days=_int("since"),
                min_referrals=_int("min_ref"),
                invited_only=(qs.get("invited") or ["0"])[0] == "1",
            )})
        if path == "/api/club/admin/audience-count":
            if not self._require_admin():
                return
            qs = parse_qs(urlparse(self.path).query)
            return self._send(200, club.audience_count((qs.get("audience") or ["all"])[0]))
        if path == "/api/club/admin/campaign-count":
            if not self._require_admin():
                return
            qs = parse_qs(urlparse(self.path).query)
            try:
                return self._send(200, club.campaign_recipients((qs.get("kind") or [""])[0]))
            except club.ClubError as e:
                return self._send(e.code, {"error": e.message})
        if path == "/api/club/admin/health":
            # Launch-control status card: API (reaching this = up), database
            # (live read+write probe), email service (config + today's volume).
            if not self._require_admin():
                return
            db_ok, members = True, 0
            try:
                with db.cursor() as c:
                    members = c.execute("SELECT COUNT(*) AS n FROM club_members").fetchone()["n"]
                db.kv_set("club_health_probe", "ok")   # proves the disk is writable
            except Exception as e:  # noqa: BLE001
                db_ok = False
                print(f"[caloria][club] health probe failed: {e}")
            return self._send(200, {
                "api": True,
                "db": db_ok,
                "db_path": config.DB_PATH,
                "members": members,
                "email_configured": config.email_ready(),
                "emails_sent_today": club.emails_sent_today(),
            })
        if path == "/api/club/admin/leaderboard":
            if not self._require_admin():
                return
            return self._send(200, {"leaderboard": club.leaderboard()})
        if path == "/api/club/admin/updates":
            if not self._require_admin():
                return
            return self._send(200, {"updates": club.updates()})
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
        if not self._subscription_gate(path):
            return
        if path == "/api/meals":
            return self._delete_meal()
        if path == "/api/me":
            return self._delete_account()
        if path == "/api/admin/community/post":
            return self._admin_delete_post()
        if path == "/api/admin/community/comment":
            return self._admin_delete_comment()
        return self._send(404, {"error": "not found"})

    def _admin_delete_post(self):
        if not self._require_admin():
            return
        qs = parse_qs(urlparse(self.path).query)
        try:
            pid = int((qs.get("post_id") or [0])[0])
        except (TypeError, ValueError):
            return self._send(400, {"error": "post_id required"})
        self._send(200, community.delete_post(pid))

    def _admin_delete_comment(self):
        if not self._require_admin():
            return
        qs = parse_qs(urlparse(self.path).query)
        try:
            cid = int((qs.get("comment_id") or [0])[0])
        except (TypeError, ValueError):
            return self._send(400, {"error": "comment_id required"})
        self._send(200, community.delete_comment(cid))

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
        # RevenueCat (Apple/Google IAP) webhook — verified by a shared secret in
        # the Authorization header rather than a body signature.
        if path == "/api/revenuecat/webhook":
            return self._rc_webhook()
        # RFC 8058 one-click unsubscribe: mail clients POST (form-encoded, not
        # JSON) to the same signed URL from the List-Unsubscribe header.
        if path == "/api/club/unsubscribe":
            self._raw_body()  # drain the request body
            qs = parse_qs(urlparse(self.path).query)
            ok = club.unsubscribe((qs.get("e") or [""])[0], (qs.get("t") or [""])[0])
            return self._send(200 if ok else 400, {"ok": ok})
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
            "/api/auth/reset-check": self._reset_check_code,
            "/api/onboarding": self._onboarding,
            "/api/onboarding/preview": self._onboarding_preview,
            "/api/app/verify/start": self._app_verify_start,
            "/api/app/verify/check": self._app_verify_check,
            "/api/app/register": self._app_register,
            "/api/app/iap/grant": self._app_iap_grant,
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
            "/api/auth/change-email": self._change_email,
            "/api/billing/checkout": self._checkout,
            "/api/billing/confirm": self._billing_confirm,
            "/api/billing/portal": self._billing_portal,
            "/api/iap/sync": self._iap_sync,
            "/api/club/join": self._club_join,
            "/api/club/answers": self._club_answers,
            "/api/club/admin/send-update": self._club_send_update,
            "/api/club/admin/send-campaign": self._club_send_campaign,
            "/api/club/admin/test-campaign": self._club_test_campaign,
            "/api/club/admin/preview-campaign": self._club_preview_campaign,
            "/api/club/admin/test-update": self._club_test_update,
            "/api/club/admin/preview-update": self._club_preview_update,
            "/api/club/admin/resume-update": self._club_resume_update,
            "/api/club/admin/resend-welcomes": self._club_resend_welcomes,
            "/api/admin/override": self._admin_override,
            "/api/admin/user/action": self._admin_user_action,
            "/api/admin/email-test": self._admin_email_test,
        }
        handler = routes.get(path)
        if handler:
            if not self._subscription_gate(path):
                return
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
        # Per-recipient cooldown + hourly cap (shared "verify" budget).
        if self._email_throttled("verify", email):
            return
        auth.resend_verification(email)
        self._send(200, {"ok": True})

    def _change_email(self, data):
        """Let a signed-in (typically UNVERIFIED) user correct the email they
        signed up with, then re-send a fresh code to the new address. Requires the
        account's own session token, so it can only change your own email."""
        if self._rate_limited("change_email", self._client_ip()):
            return
        u = self._require_user()
        if not u:
            return
        # Per-recipient cooldown + hourly cap (shared "verify" budget) so the new
        # address can't be used to fire off a burst of verification emails.
        if self._email_throttled("verify", str(data.get("email", ""))):
            return
        try:
            new_email = auth.change_email(u["id"], data.get("email", ""))
        except auth.AuthError as e:
            return self._send(e.code, {"error": e.message})
        self._send(200, {"ok": True, "email": new_email,
                         "user": auth.public_user(self._user())})

    def _forgot_password(self, data):
        if self._rate_limited("forgot", self._client_ip()):
            return
        email = str(data.get("email", ""))
        # Per-recipient cooldown + hourly cap. Keyed on the submitted address only
        # (frequency-based), so a 429 here still reveals nothing about existence.
        if self._email_throttled("reset", email):
            return
        auth.request_reset(email)
        # Always 200 — never reveal whether the email exists (no enumeration).
        self._send(200, {"ok": True})

    def _reset_password(self, data):
        # Code-based reset: {email, code, password}. Rate-limited like verify.
        if self._rate_limited("verify_code", self._client_ip()):
            return
        try:
            ok = auth.reset_password_with_code(
                str(data.get("email", "")), str(data.get("code", "")),
                str(data.get("password", "")),
            )
        except auth.AuthError as e:
            return self._send(e.code, {"error": e.message})
        if not ok:
            return self._send(400, {
                "error": "That code is incorrect or has expired. Please check the code or request a new one.",
                "expired": True,
            })
        # `ok` is an auth payload {token, user}: log the user straight in so they
        # never have to re-type the new password (avoids password-manager autofill
        # submitting the OLD saved password). Still backward compatible: `ok` is
        # truthy and also carries `token`.
        self._send(200, ok)

    def _reset_check_code(self, data):
        """Non-burning check so the UI can advance to the new-password step."""
        if self._rate_limited("verify_code", self._client_ip()):
            return
        ok = auth.check_reset_code(str(data.get("email", "")), str(data.get("code", "")))
        if not ok:
            return self._send(400, {"error": "That code is incorrect or has expired."})
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

    def _onboarding_preview(self, data):
        """PUBLIC — compute targets from the questionnaire answers WITHOUT an
        account or saving anything, so the results screen can be shown before
        sign-up (new onboarding order). Uses the exact same nutrition_engine math
        as /api/onboarding; the answers are saved later, once the account exists."""
        profile = data.get("profile") or data
        try:
            targets = auth.compute_targets(profile)
        except Exception as e:  # noqa: BLE001 — never 500 the funnel
            print(f"[caloria] onboarding preview failed: {e}")
            return self._send(400, {"error": "We couldn't build your plan. Please try again."})
        self._send(200, {"targets": targets})

    # ---- NATIVE iOS onboarding (verify-first) ----
    def _app_gate(self) -> bool:
        if not appauth.app_secret_ok(self.headers.get("X-Caloria-App", "")):
            self._send(403, {"error": "app client not authorized"})
            return False
        return True

    def _app_verify_start(self, data):
        if not self._app_gate():
            return
        if self._rate_limited("app_verify", self._client_ip()):
            return
        # Per-recipient cooldown + hourly cap (shared "verify" budget with the web
        # resend/forgot/change-email paths) so the native app can't bypass the
        # email-abuse protection. Clear 429 + Retry-After when throttled.
        if self._email_throttled("verify", str(data.get("email", ""))):
            return
        try:
            self._send(200, appauth.start_verification(data.get("email"), data.get("name", "")))
        except appauth.AppAuthError as e:
            self._send(e.code, {"error": e.message})

    def _app_verify_check(self, data):
        if not self._app_gate():
            return
        try:
            self._send(200, appauth.check_code(data.get("email"), data.get("code")))
        except appauth.AppAuthError as e:
            self._send(e.code, {"error": e.message})

    def _app_register(self, data):
        if not self._app_gate():
            return
        try:
            self._send(200, appauth.register(
                data.get("email"), data.get("password"), data.get("name", ""), data.get("profile")))
        except appauth.AppAuthError as e:
            self._send(e.code, {"error": e.message})

    def _app_iap_grant(self, data):
        if not self._app_gate():
            return
        u = self._require_user()
        if not u:
            return
        self._send(200, appauth.grant_premium(u))

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
        if not imagevalid.is_valid_image_data_url(image):
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
            print(f"[caloria] mealplan failed for user {u['id']}: {e}")
            return self._send(503, {"error": "Couldn't build your meal plan right now. Please try again."})
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
            print(f"[caloria] meal regen failed for user {u['id']}: {e}")
            return self._send(503, {"error": "Couldn't regenerate that meal right now. Please try again."})
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
            print(f"[caloria] workout gen failed for user {u['id']}: {e}")
            return self._send(503, {"error": "Couldn't build your workout right now. Please try again."})
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
            print(f"[caloria] coach failed for user {u['id']}: {e}")   # detail stays server-side
            return self._send(503, {"error": "The coach is unavailable right now. Please try again."})
        usage.record(u["id"], "coach", email=u["email"], plan=u["plan"], is_admin=admin)
        self._send(200, {"reply": answer})

    # ---- Caloria Club (founding members) ----
    def _club_join(self, data):
        if self._rate_limited("club_join", self._client_ip()):
            return
        try:
            res = club.join(str(data.get("email", "")), str(data.get("ref", "")))
        except club.ClubError as e:
            return self._send(e.code, {"error": e.message})
        self._send(200, res)

    def _club_answers(self, data):
        """Save one or more founding-onboarding answers for a member (by code)."""
        if self._rate_limited("club_answers", self._client_ip()):
            return
        try:
            res = club.save_answers(str(data.get("code", "")), data)
        except club.ClubError as e:
            return self._send(e.code, {"error": e.message})
        self._send(200, res)

    def _club_send_update(self, data):
        if not self._require_admin():
            return
        try:
            res = club.send_update(str(data.get("subject", "")), str(data.get("message", "")),
                                   str(data.get("audience", "all")))
        except club.ClubError as e:
            return self._send(e.code, {"error": e.message})
        self._send(200, res)

    def _club_send_campaign(self, data):
        """Send a pre-built campaign (Tomorrow / Early Access) to all members."""
        if not self._require_admin():
            return
        try:
            res = club.send_campaign(str(data.get("kind", "")))
        except club.ClubError as e:
            return self._send(e.code, {"error": e.message})
        self._send(200, res)

    def _club_test_campaign(self, data):
        """Send ONE copy of a pre-built campaign (e.g. Follow-up) to the signed-in
        admin only — never logged as a campaign, so it doesn't affect dedup."""
        u = self._require_admin()
        if not u:
            return
        try:
            res = club.test_campaign(str(data.get("kind", "")), u["email"])
        except club.ClubError as e:
            return self._send(e.code, {"error": e.message})
        except Exception as e:  # noqa: BLE001 — surface provider errors cleanly
            print(f"[caloria][club] campaign test email failed: {e}")
            return self._send(503, {"error": "The test email could not be sent. Please try again."})
        self._send(200, res)

    def _club_preview_campaign(self, data):
        """Render a pre-built campaign exactly as members receive it (for the
        admin preview iframe). Sends nothing."""
        u = self._require_admin()
        if not u:
            return
        try:
            res = club.preview_campaign(str(data.get("kind", "")), u["email"])
        except club.ClubError as e:
            return self._send(e.code, {"error": e.message})
        self._send(200, res)

    def _club_test_update(self, data):
        """Send one personalized test email to the signed-in admin only."""
        u = self._require_admin()
        if not u:
            return
        try:
            res = club.send_test(str(data.get("subject", "")), str(data.get("message", "")),
                                 u["email"])
        except club.ClubError as e:
            return self._send(e.code, {"error": e.message})
        except Exception as e:  # noqa: BLE001 — surface provider errors cleanly
            print(f"[caloria][club] test email failed: {e}")
            return self._send(503, {"error": "The test email could not be sent. Please try again."})
        self._send(200, res)

    def _club_preview_update(self, data):
        if not self._require_admin():
            return
        try:
            res = club.preview_update(str(data.get("subject", "")), str(data.get("message", "")))
        except club.ClubError as e:
            return self._send(e.code, {"error": e.message})
        self._send(200, res)

    def _club_resend_welcomes(self, data):
        """Re-attempt welcome letters for members who never got one (rate-limit days)."""
        if not self._require_admin():
            return
        try:
            self._send(200, club.resend_welcomes())
        except club.ClubError as e:
            self._send(e.code, {"error": e.message})

    def _club_resume_update(self, data):
        if not self._require_admin():
            return
        try:
            res = club.resume_update(int(data.get("id", 0)))
        except club.ClubError as e:
            return self._send(e.code, {"error": e.message})
        except (TypeError, ValueError):
            return self._send(400, {"error": "invalid campaign id"})
        self._send(200, res)

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
            elif action == "grant_founding":
                res = admin.set_founding(email, True)
            elif action == "revoke_founding":
                res = admin.set_founding(email, False)
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
        # No currency handling here — Stripe Adaptive Pricing presents each
        # customer their local currency at checkout from the single base price.
        try:
            url = billing.create_checkout(auth.public_user(u), data.get("interval", "monthly"),
                                          return_base=self.headers.get("Origin", ""))
        except billing.BillingError as e:
            return self._send(503, {"error": str(e)})
        self._send(200, {"url": url})

    def _billing_confirm(self, data):
        """Post-checkout: activate Premium immediately by verifying the session
        with Stripe (webhook-independent). Returns the refreshed user."""
        u = self._require_user()
        if not u:
            return
        try:
            billing.confirm_checkout(u, str(data.get("session_id", "")))
        except Exception as e:  # noqa: BLE001 — never 500 the return flow
            print(f"[caloria] billing confirm error: {e}")
        self._send(200, {"user": auth.public_user(self._user())})   # refreshed post-activation

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

    def _rc_webhook(self):
        """RevenueCat webhook — the durable source of truth for IAP entitlements
        (renewals, expirations, refunds, cross-device). Verified by the shared
        Authorization secret configured in the RevenueCat dashboard."""
        raw = self._raw_body()
        try:
            if not revenuecat.verify_webhook_auth(self.headers.get("Authorization", "")):
                return self._send(401, {"error": "bad webhook auth"})
        except revenuecat.RevenueCatError as e:
            return self._send(503, {"error": str(e)})
        try:
            payload = json.loads(raw.decode() or "{}")
        except (json.JSONDecodeError, ValueError):
            return self._send(400, {"error": "invalid JSON body"})
        try:
            revenuecat.handle_event(payload)
        except Exception as e:  # noqa: BLE001 — never 500 a webhook; RC would retry-storm
            print(f"[caloria][rc] webhook handling error: {e}")
        self._send(200, {"received": True})

    def _iap_sync(self, data):
        """Post-purchase: verify the signed-in user's entitlements directly with
        RevenueCat and unlock immediately (webhook-independent), then return the
        refreshed user — mirrors the Stripe confirm flow."""
        u = self._require_user()
        if not u:
            return
        try:
            revenuecat.sync_subscriber(u)
        except Exception as e:  # noqa: BLE001 — never 500 the return flow
            print(f"[caloria][rc] iap sync error: {e}")
        self._send(200, {"user": auth.public_user(self._user())})   # refreshed post-activation


def main():
    db.init_db()
    usage.init()
    club.init()   # backfill canonical emails for pre-existing club members
    # Retention-email scheduler (daemon). Only auto-sends when EMAIL_RETENTION_ENABLED.
    threading.Thread(target=mailer.run_scheduler, daemon=True).start()
    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(f"Caloria API on http://{config.HOST}:{config.PORT}")
    print(f"  OpenAI: {'configured' if config.openai_ready() else 'NOT configured'} "
          f"(vision: {config.OPENAI_VISION_MODEL}, text: {config.OPENAI_TEXT_MODEL})")
    print(f"  USDA:   {'DEMO_KEY (rate-limited)' if config.USDA_API_KEY == 'DEMO_KEY' else 'configured'}")
    if config.stripe_ready():
        mode = billing._key_mode()
        print(f"  Stripe: configured ({mode} mode)")
        prod = "caloriaclub.com" in (config.APP_BASE_URL or "")
        if prod and mode == "test":
            print("  [WARNING] Production APP_BASE_URL with a TEST Stripe key — real "
                  "payments will fail. Use sk_live_… and live Price IDs in production.")
        elif not prod and mode == "live":
            print("  [WARNING] LIVE Stripe key on a non-production APP_BASE_URL — this can "
                  "create REAL charges. Use sk_test_… outside production.")
    else:
        print("  Stripe: NOT configured")
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
