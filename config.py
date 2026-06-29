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
STRIPE_PRICE_MONTHLY = os.environ.get("STRIPE_PRICE_MONTHLY", "").strip()  # price_...
STRIPE_PRICE_YEARLY = os.environ.get("STRIPE_PRICE_YEARLY", "").strip()
PRICE_MONTHLY_DISPLAY = "$19.99"
PRICE_YEARLY_DISPLAY = "$99"
# Revenue per active subscriber used for MRR in the admin analytics.
MRR_PER_SUBSCRIBER = float(os.environ.get("MRR_PER_SUBSCRIBER", "19.99"))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").strip()

# --- Transactional email (Resend) ---
# https://resend.com/api-keys — used for email verification & password reset.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Caloria <onboarding@resend.dev>").strip()
# Version of the Terms/Privacy a user accepts at signup (for consent evidence).
POLICY_VERSION = os.environ.get("POLICY_VERSION", "2026-06-17").strip()
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "15"))
VERIFY_TOKEN_TTL_HOURS = int(os.environ.get("VERIFY_TOKEN_TTL_HOURS", "24"))
# 6-digit email verification code: lifetime and max wrong attempts before it's burned.
VERIFY_CODE_TTL_MINUTES = int(os.environ.get("VERIFY_CODE_TTL_MINUTES", "15"))
VERIFY_CODE_MAX_ATTEMPTS = int(os.environ.get("VERIFY_CODE_MAX_ATTEMPTS", "5"))
RESET_TOKEN_TTL_HOURS = int(os.environ.get("RESET_TOKEN_TTL_HOURS", "1"))
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
# Lock CORS to your frontend origin in production (e.g. https://app.caloria.com).
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*").strip()
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


def email_ready() -> bool:
    return bool(RESEND_API_KEY)


def turnstile_ready() -> bool:
    return bool(TURNSTILE_SECRET and TURNSTILE_SITE_KEY)
