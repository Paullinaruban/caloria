"""Configuration & lightweight .env loader for the Caloria backend.

Reads secrets from environment variables, falling back to a `backend/.env`
file if present.  Never hard-code keys — see .env.example.
"""
import os
import secrets
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent / ".env"


def _load_dotenv(path: Path) -> None:
    """Minimal .env parser (KEY=VALUE lines). Real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv(_ENV_PATH)

# --- Vision & generation: OpenAI ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
# `OPENAI_MODEL` stays the default for any general use.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o").strip()
# Vision (meal photo recognition) uses gpt-4o-mini: with detail:"low" + our
# structured prompt + USDA nutrition lookup it recognises food just as well as
# gpt-4o for ~88% less cost (~$0.0008 vs ~$0.0066 per scan, measured). Override
# with OPENAI_VISION_MODEL=gpt-4o only if you ever need maximum recognition.
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini").strip()
# All text-only generation (AI coach, meal-quality coaching text) uses the cheap,
# fast model — ~15x cheaper than gpt-4o with no meaningful quality loss for chat.
OPENAI_TEXT_MODEL = os.environ.get("OPENAI_TEXT_MODEL", "gpt-4o-mini").strip()
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "dall-e-3").strip()
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
ENABLE_MEAL_IMAGES = os.environ.get("ENABLE_MEAL_IMAGES", "false").lower() == "true"

# --- Nutrition: USDA FoodData Central ---
USDA_API_KEY = os.environ.get("USDA_FDC_API_KEY", "DEMO_KEY").strip()
USDA_BASE_URL = "https://api.nal.usda.gov/fdc/v1"
USDA_DATA_TYPES = ["Foundation", "SR Legacy"]  # reliable per-100g profiles

# --- Stripe subscriptions ---
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
# Explicit Stripe Price ids (price_...). When set, checkout uses EXACTLY that Price
# and OVERRIDES auto-creation. Set STRIPE_PRICE_MONTHLY to your $19/month Price id
# and STRIPE_PRICE_YEARLY to your $99/year Price id. Leave blank to auto-create the
# Price from the amounts below. See explicit_price_id() and billing.create_checkout().
STRIPE_PRICE_MONTHLY = os.environ.get("STRIPE_PRICE_MONTHLY", "").strip()   # the $19/month Price id
STRIPE_PRICE_YEARLY  = os.environ.get("STRIPE_PRICE_YEARLY", "").strip()    # the $99/year Price id

# ============================================================================
# SUBSCRIPTION PRICING — SINGLE SOURCE OF TRUTH
# ----------------------------------------------------------------------------
# Monthly is a flat $19/month, always presented as a discount: $25 struck through
# with $19 emphasized. Every surface — display strings, the Stripe amount in every
# currency, /api/config, the landing hero/paywalls, and admin MRR — reads the
# helpers below, so one change updates everything at once. All env-overridable.
# The actual Stripe charge uses STRIPE_PRICE_MONTHLY (your $19 Price id); if unset,
# a $19 Price is auto-created from MONTHLY_PRICE_USD. Yearly is fixed at $99.
# ============================================================================
import datetime as _dt

MONTHLY_PRICE_USD_BASE = float(os.environ.get("MONTHLY_PRICE_USD", "19"))     # actual charged price
MONTHLY_COMPARE_USD    = float(os.environ.get("MONTHLY_COMPARE_USD", "25"))   # struck-through "was" price
YEARLY_PRICE_USD       = float(os.environ.get("YEARLY_PRICE_USD", "99"))


def promo_active(now=None) -> bool:
    """The discount ($25 → $19) is always presented to users."""
    return True


def monthly_price_usd(now=None) -> float:
    """Current monthly price in USD (flat $19)."""
    return MONTHLY_PRICE_USD_BASE


def _fmt_price(symbol, amt) -> str:
    """'$19' for whole amounts, '$19.50' when cents are present."""
    return f"{symbol}{int(amt)}" if float(amt).is_integer() else f"{symbol}{amt:.2f}"


# Per-USD multiplier to derive each market's MONTHLY amount from the USD price.
_MONTHLY_FX_PER_USD = {"USD": 1.0, "EUR": 1.0, "GBP": 1.0, "THB": 35.0}
_YEARLY_MINOR = {"USD": 9900, "THB": 349900, "EUR": 9900, "GBP": 8400}


def _monthly_minor_units(currency: str, now=None) -> int:
    """Current monthly amount in the currency's smallest unit."""
    amt = monthly_price_usd(now) * _MONTHLY_FX_PER_USD.get(currency, 1.0)
    minor = amt * 100
    if currency == "THB":                 # keep THB to whole baht
        minor = round(minor / 100) * 100
    return int(round(minor))


def price_monthly_display(now=None) -> str:
    return _fmt_price("$", monthly_price_usd(now))               # "$19"


def price_monthly_compare_display(now=None):
    """Struck-through 'was' price shown next to the monthly price ($25)."""
    return _fmt_price("$", MONTHLY_COMPARE_USD)                  # "$25"


PRICE_YEARLY_DISPLAY = _fmt_price("$", YEARLY_PRICE_USD)   # "$99" (static)


# Back-compat: keep the old module attribute names working, resolved LIVE so the
# promo reversion needs no redeploy (PEP 562 module __getattr__).
def __getattr__(name):
    if name == "MONTHLY_PRICE_USD":
        return monthly_price_usd()
    if name == "PRICE_MONTHLY_DISPLAY":
        return price_monthly_display()
    if name == "PRICE_MONTHLY_COMPARE_DISPLAY":
        return price_monthly_compare_display()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# --- RevenueCat (Apple In-App Purchase now; Google Play later) ---
# The Apple *public* SDK key (starts with "appl_") is safe to expose to the app
# and is served to it via /api/config. The *secret* v1 REST key and the webhook
# Authorization value are server-only — never send them to the client.
REVENUECAT_APPLE_KEY   = os.environ.get("REVENUECAT_APPLE_KEY", "").strip()    # appl_... (public SDK key, iOS)
REVENUECAT_GOOGLE_KEY  = os.environ.get("REVENUECAT_GOOGLE_KEY", "").strip()   # goog_... (public SDK key, Android — later)
REVENUECAT_SECRET_KEY  = os.environ.get("REVENUECAT_SECRET_KEY", "").strip()   # sk_... server REST key (verify + read)
REVENUECAT_WEBHOOK_AUTH = os.environ.get("REVENUECAT_WEBHOOK_AUTH", "").strip()# shared secret you set as the webhook Authorization header
REVENUECAT_ENTITLEMENT = os.environ.get("REVENUECAT_ENTITLEMENT", "premium").strip()  # entitlement id in RevenueCat

# --- Multi-currency pricing (USD, THB, EUR, GBP) ---
# Localized Stripe Price ids per currency + interval. Create these prices in your
# Stripe dashboard and paste their ids here. A currency with a missing price id
# is simply not offered — checkout always falls back to USD.
SUPPORTED_CURRENCIES = ("USD", "THB", "EUR", "GBP")

STRIPE_PRICE_IDS = {
    ("monthly", "USD"): os.environ.get("STRIPE_PRICE_MONTHLY_USD", "").strip(),
    ("yearly",  "USD"): os.environ.get("STRIPE_PRICE_YEARLY_USD", "").strip(),
    ("monthly", "THB"): os.environ.get("STRIPE_PRICE_MONTHLY_THB", "").strip(),
    ("yearly",  "THB"): os.environ.get("STRIPE_PRICE_YEARLY_THB", "").strip(),
    ("monthly", "EUR"): os.environ.get("STRIPE_PRICE_MONTHLY_EUR", "").strip(),
    ("yearly",  "EUR"): os.environ.get("STRIPE_PRICE_YEARLY_EUR", "").strip(),
    ("monthly", "GBP"): os.environ.get("STRIPE_PRICE_MONTHLY_GBP", "").strip(),
    ("yearly",  "GBP"): os.environ.get("STRIPE_PRICE_YEARLY_GBP", "").strip(),
}

# Rough country -> default-currency map (used when the client sends a country
# instead of an explicit currency preference).
_COUNTRY_CURRENCY = {
    "TH": "THB", "GB": "GBP",
    "AT": "EUR", "BE": "EUR", "CY": "EUR", "EE": "EUR", "FI": "EUR", "FR": "EUR",
    "DE": "EUR", "GR": "EUR", "IE": "EUR", "IT": "EUR", "LV": "EUR", "LT": "EUR",
    "LU": "EUR", "MT": "EUR", "NL": "EUR", "PT": "EUR", "SK": "EUR", "SI": "EUR", "ES": "EUR",
}


def normalize_currency(currency: str) -> str:
    c = (currency or "").upper()
    return c if c in SUPPORTED_CURRENCIES else "USD"


def stripe_price_id(interval: str, currency: str) -> str:
    return STRIPE_PRICE_IDS.get((interval, normalize_currency(currency)), "")


def explicit_price_id(interval: str, now=None) -> str:
    """Explicit Stripe Price id to use for checkout (overrides auto-creation).
    Set STRIPE_PRICE_MONTHLY to your $19/month Price id and STRIPE_PRICE_YEARLY to
    the $99/year id. Returns '' → the Price auto-creates from the amount in config."""
    if interval == "yearly":
        return STRIPE_PRICE_YEARLY
    if interval == "monthly":
        return STRIPE_PRICE_MONTHLY
    return ""


def currency_for_country(country: str) -> str:
    return _COUNTRY_CURRENCY.get((country or "").upper(), "USD")


# --- Base currency for NEW checkouts --------------------------------------
# Stripe Adaptive Pricing only converts a price whose currency is one of your
# SETTLEMENT currencies. A Thailand account settles in THB, so the base price
# must be THB — Stripe then presents/charges each buyer in their own local
# currency, converting from THB. Change via env only if your settlement currency
# changes; setting BASE_CURRENCY=USD reverts to the previous behaviour.
BASE_CURRENCY = os.environ.get("BASE_CURRENCY", "THB").upper()
if BASE_CURRENCY not in SUPPORTED_CURRENCIES:
    BASE_CURRENCY = "USD"

# Amount (in the currency's smallest unit) used ONLY when auto-creating a base
# price and no explicit Stripe Price id is set. Monthly derives from the CURRENT
# monthly price; yearly is fixed. Override per
# market via env, e.g. PRICE_MONTHLY_THB_AMOUNT=52500 (satang → ฿525.00).
def base_amount(interval: str, currency: str, now=None) -> int:
    currency = normalize_currency(currency)
    if interval == "monthly":
        default = _monthly_minor_units(currency, now)
    else:
        default = _YEARLY_MINOR.get(currency, _YEARLY_MINOR["USD"])
    try:
        return int(os.environ.get(f"PRICE_{interval.upper()}_{currency}_AMOUNT", default))
    except ValueError:
        return default


# Explicit per-currency prices attached to the base Price via Stripe
# `currency_options`. These OVERRIDE Adaptive Pricing for the listed currencies,
# so customers there pay THIS EXACT amount, while Adaptive Pricing converts the
# base for every OTHER currency. Monthly amounts follow the CURRENT price
#; yearly is fixed. Env-overridable per market.
def price_currency_options(interval: str, base_currency: str, now=None) -> dict:
    """Explicit per-currency amounts for `interval`, EXCLUDING the base currency
    (Stripe rejects a currency_option equal to the price's own currency)."""
    base = normalize_currency(base_currency)
    if interval == "monthly":
        amounts = {c: _monthly_minor_units(c, now) for c in ("USD", "EUR", "GBP")}
    else:
        amounts = {"USD": 9900, "EUR": 9900, "GBP": 8400}
    out = {}
    for cur, amt in amounts.items():
        if cur == base:
            continue
        try:
            out[cur] = int(os.environ.get(f"PRICE_{interval.upper()}_{cur}_AMOUNT", amt))
        except ValueError:
            out[cur] = amt
    return out


def base_display() -> dict:
    """Monthly/yearly display strings for the BASE_CURRENCY (shown pre-checkout)."""
    disp = currency_display()
    d = disp.get(BASE_CURRENCY, disp["USD"])
    return {"symbol": d["symbol"], "monthly": d["monthly"], "yearly": d["yearly"]}


def currency_display(now=None) -> dict:
    """Per-currency symbol + monthly/yearly display strings for the UI. Monthly
    reflects the CURRENT price."""
    syms = {"USD": "$", "THB": "฿", "EUR": "€", "GBP": "£"}
    yearly = {"USD": "$99", "THB": "฿3,499", "EUR": "€99", "GBP": "£84"}
    out = {}
    for cur, sym in syms.items():
        monthly = _fmt_price(sym, _monthly_minor_units(cur, now) / 100)
        out[cur] = {
            "symbol": sym,
            "monthly": os.environ.get(f"PRICE_MONTHLY_{cur}_DISPLAY", monthly).strip(),
            "yearly": os.environ.get(f"PRICE_YEARLY_{cur}_DISPLAY", yearly[cur]).strip(),
        }
    return out


def available_currencies() -> list:
    """USD is always offered (it is the universal fallback). THB/EUR/GBP appear
    only when BOTH their monthly & yearly price ids are set, so the displayed
    currency always matches what the customer is actually charged."""
    avail = ["USD"]
    for cur in ("THB", "EUR", "GBP"):
        if stripe_price_id("monthly", cur) and stripe_price_id("yearly", cur):
            avail.append(cur)
    return avail
# Revenue per active subscriber used for MRR in the admin analytics.
MRR_PER_SUBSCRIBER = float(os.environ.get("MRR_PER_SUBSCRIBER", str(monthly_price_usd())))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").strip()

# --- Transactional email (Resend) ---
# https://resend.com/api-keys — used for email verification & password reset.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Caloria <onboarding@resend.dev>").strip()
# Optional Reply-To — a real, monitored inbox. Replies to a noreply@ sender
# bounce; a working Reply-To both feels personal and helps deliverability.
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "").strip()
# Version of the Terms/Privacy a user accepts at signup (for consent evidence).
POLICY_VERSION = os.environ.get("POLICY_VERSION", "2026-06-17").strip()
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "15"))
# Shorter per-attempt timeout for emails sent INLINE during a signup/login/resend
# request (verification codes). Keeps the request from hanging if the provider is
# slow, while the send still completes in-request (not a fire-and-forget thread
# that a suspended instance could drop). Normal sends return in well under 1s.
EMAIL_TIMEOUT_INTERACTIVE = int(os.environ.get("EMAIL_TIMEOUT_INTERACTIVE", "8"))
VERIFY_TOKEN_TTL_HOURS = int(os.environ.get("VERIFY_TOKEN_TTL_HOURS", "24"))
# 6-digit email verification code: lifetime and max wrong attempts before it's burned.
VERIFY_CODE_TTL_MINUTES = int(os.environ.get("VERIFY_CODE_TTL_MINUTES", "15"))
VERIFY_CODE_MAX_ATTEMPTS = int(os.environ.get("VERIFY_CODE_MAX_ATTEMPTS", "5"))
RESET_TOKEN_TTL_HOURS = int(os.environ.get("RESET_TOKEN_TTL_HOURS", "1"))
# Password reset now uses a 6-digit code (same UX as email verification).
RESET_CODE_TTL_MINUTES = int(os.environ.get("RESET_CODE_TTL_MINUTES", "15"))
RESET_CODE_MAX_ATTEMPTS = int(os.environ.get("RESET_CODE_MAX_ATTEMPTS", "5"))
# Require a verified email before AI features / premium unlock. Strongly
# recommended for launch (blocks unverified bots from spending OpenAI credits).
REQUIRE_EMAIL_VERIFICATION = os.environ.get("REQUIRE_EMAIL_VERIFICATION", "true").lower() == "true"
# Retention emails (Sunday Reset / midweek / re-engagement / milestone). OFF by
# default — the background scheduler will not auto-send until you opt in.
EMAIL_RETENTION_ENABLED = os.environ.get("EMAIL_RETENTION_ENABLED", "false").lower() == "true"

# --- Bot protection: Cloudflare Turnstile ---
# https://dash.cloudflare.com/?to=/:account/turnstile — site key is public; secret is server-side.
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "").strip()
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "").strip()

# --- Sessions / CORS / proxy ---
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))
# Lock CORS to your frontend origin. When ALLOWED_ORIGIN isn't set explicitly we
# default to APP_BASE_URL (your production site) rather than a wildcard, so a
# correctly-configured deployment is locked to the real domain out of the box.
# Set ALLOWED_ORIGIN="*" explicitly only for local/dev use.
# A COMMA-SEPARATED list is supported so the live site AND a preview deployment
# can both call the same backend. Entries may include a WILDCARD subdomain so a
# staging URL doesn't have to be hard-coded — e.g.
#   ALLOWED_ORIGIN="https://caloriaclub.com,https://*.netlify.app"
# matches any <anything>.netlify.app origin (Netlify assigns random subdomains).
ALLOWED_ORIGIN = (os.environ.get("ALLOWED_ORIGIN", "").strip()
                  or APP_BASE_URL.rstrip("/"))
ALLOWED_ORIGINS = {o.strip().rstrip("/") for o in ALLOWED_ORIGIN.split(",") if o.strip()}


def _origin_matches(origin: str, pattern: str) -> bool:
    """Exact match, or a single-wildcard host pattern like https://*.netlify.app
    (prefix + suffix must both match, and the suffix must be dotted so
    'https://*.netlify.app' can never match 'https://evilnetlify.app')."""
    if pattern == origin:
        return True
    if "*" in pattern:
        prefix, _, suffix = pattern.partition("*")
        return (origin.startswith(prefix) and origin.endswith(suffix)
                and len(origin) >= len(prefix) + len(suffix))
    return False


def cors_origin_for(request_origin: str) -> str:
    """Which Access-Control-Allow-Origin to return for this request. Reflects the
    caller's Origin when it matches an allow-listed entry (exact or wildcard);
    otherwise falls back to the first configured origin. '*' allows any (dev)."""
    if "*" in ALLOWED_ORIGINS:
        return "*"
    ro = (request_origin or "").rstrip("/")
    if ro and any(_origin_matches(ro, pat) for pat in ALLOWED_ORIGINS):
        return request_origin
    return ALLOWED_ORIGIN.split(",")[0].strip()
# Trust X-Forwarded-For (only enable behind a reverse proxy you control).
TRUST_PROXY = os.environ.get("TRUST_PROXY", "false").lower() == "true"

# --- Server ---
# Bind 0.0.0.0 so cloud hosts (Render, etc.) can route traffic to the container.
# PORT is the platform-standard env var (Render injects it); CALORIA_PORT and the
# 8787 default keep local development unchanged.
HOST = os.environ.get("CALORIA_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("CALORIA_PORT", "8787")))
DB_PATH = os.environ.get("CALORIA_DB", str(Path(__file__).resolve().parent / "caloria.db"))

OPENAI_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT", "90"))
USDA_TIMEOUT = int(os.environ.get("USDA_TIMEOUT", "20"))
LOW_CONFIDENCE_THRESHOLD = float(os.environ.get("LOW_CONFIDENCE_THRESHOLD", "0.6"))

# No free tier: unpaid accounts get ZERO AI access (no free scans/messages).
FREE_SCAN_LIMIT = int(os.environ.get("FREE_SCAN_LIMIT", "0"))

# --- Premium monthly usage caps (INTERNAL — never shown to users) ---
# Cost / abuse / stability safeguard. Enforced server-side only; the UI never
# displays counters, quotas, or remaining usage. Admins can raise these per user
# at runtime (see usage.py) without code changes.
PREMIUM_SCAN_LIMIT = int(os.environ.get("PREMIUM_SCAN_LIMIT", "100"))
PREMIUM_COACH_LIMIT = int(os.environ.get("PREMIUM_COACH_LIMIT", "100"))

# Optional outbound owner-alert webhook (Slack/Discord/email-relay/etc.). If set,
# threshold alerts (50/75/90/100%) are POSTed here as JSON. Always logged + stored
# regardless. Leave blank to rely on the admin dashboard + server log only.
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()

# --- Developer / admin testing mode ---
# DEV_UNLIMITED=true grants EVERY account full premium access with no paywall,
# no Stripe, no usage limits — for local testing/owner use. Defaults off so
# production stays gated. ADMIN_EMAILS is a comma-separated allowlist of
# accounts that are always premium even when DEV_UNLIMITED is off.
DEV_UNLIMITED = os.environ.get("DEV_UNLIMITED", "false").lower() == "true"
ADMIN_EMAILS = {
    e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()
}

# No free trials. 0 disables the trial entirely (paid from day one).
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "0"))

# --- Founding Member badge (private launch) ---
# The private invite list: accounts that sign up (or log in) with one of these
# emails receive a PERMANENT Founding Member badge. Paste the invited emails
# here, comma-separated — that is how you "mark" invited users. Case/spacing
# insensitive. The badge, once granted, is stored in the DB and never revoked
# automatically, even if the email is later removed from this list.
FOUNDING_MEMBER_EMAILS = {
    e.strip().lower() for e in os.environ.get("FOUNDING_MEMBER_EMAILS", "").split(",") if e.strip()
}
# Hard cutoff (UTC, YYYY-MM-DD). After this day ends, the badge can NEVER be
# auto-granted again — the private launch is closed forever. Blank = no cutoff.
FOUNDING_MEMBER_DEADLINE = os.environ.get("FOUNDING_MEMBER_DEADLINE", "").strip()


def founding_window_open() -> bool:
    """True while new Founding Member badges may still be granted. Enforces the
    private-launch cutoff so the badge is genuinely unobtainable afterwards."""
    if not FOUNDING_MEMBER_DEADLINE:
        return True
    import datetime
    try:
        deadline = datetime.datetime.strptime(FOUNDING_MEMBER_DEADLINE, "%Y-%m-%d")
    except ValueError:
        return True  # misconfigured date must not silently close the window
    # Allowed through the END of the deadline day (UTC).
    return datetime.datetime.utcnow() <= deadline + datetime.timedelta(days=1)

# Server-side secret for hashing/session salting. Stable across restarts if set.
APP_SECRET = os.environ.get("APP_SECRET", "").strip() or secrets.token_hex(32)


def _is_placeholder(v: str) -> bool:
    """True for obvious unset/placeholder secret values (e.g. 'sk-REPLACE...')."""
    low = (v or "").lower()
    return (not v) or low.startswith("sk-replace") or "your_key" in low or "replace" in low

def openai_ready() -> bool:
    # A non-empty key isn't enough — reject the shipped placeholder so the app
    # honestly reports AI as NOT configured instead of failing with 401s.
    return bool(OPENAI_API_KEY) and not _is_placeholder(OPENAI_API_KEY)


def stripe_ready() -> bool:
    return bool(STRIPE_SECRET_KEY)


# Native iOS app client gate: when set, the /api/app/* endpoints require this
# value in the X-Caloria-App header (keeps random web callers out; the app ships
# it). Leave blank in dev to keep them open.
APP_CLIENT_SECRET = os.environ.get("APP_CLIENT_SECRET", "").strip()


def revenuecat_ready() -> bool:
    """Server can verify purchases + trust webhooks once the secret key is set."""
    return bool(REVENUECAT_SECRET_KEY)


def email_ready() -> bool:
    return bool(RESEND_API_KEY)


def turnstile_ready() -> bool:
    return bool(TURNSTILE_SECRET and TURNSTILE_SITE_KEY)
