"""Lucky Wheel promotional system.

A NEW visitor spins a weighted wheel and wins a discounted FIRST billing period on
one plan (Yearly / 3-month / Monthly), then renews at the regular price. The prize
is chosen HERE on the backend with a secure weighted algorithm (never a frontend
RNG, never equal odds) and written to `promo_redemptions` BEFORE the result is
shown, so the frontend animation merely stops on the prize the server already
picked.

Security / eligibility:
  • The anonymous spin is keyed by an unguessable `wheel_session` token; a session
    that already has an active (pending, unexpired) redemption gets the SAME prize
    back — refreshing or re-opening never rerolls.
  • The BINDING new-customer gate is enforced at CHECKOUT, not at spin, via
    validate_redemption(): the discount only applies if the *account* that reaches
    checkout is a genuine new customer (no prior subscription / redemption). So
    clearing cookies / incognito / switching browsers cannot obtain the discount —
    it is validated against account records, not the cookie.

Everything here is additive and isolated from users/auth/billing: it only reads the
users row for eligibility and writes its own `promo_redemptions` / `wheel_events`
tables. The actual Stripe coupon is minted lazily at checkout by billing.py.
"""
from __future__ import annotations

import datetime
import json
import secrets

import config
import db

_TS_FMT = "%Y-%m-%d %H:%M:%S"
# Events accepted by the funnel log (the frontend may send any; we store all, but
# these are the canonical ones surfaced in the admin conversion report).
FUNNEL_EVENTS = (
    "popup_shown", "spin_initiated", "prize_selected", "winning_shown",
    "join_clicked", "onboarding_started", "registration_completed",
    "checkout_opened", "payment_success", "payment_failed", "subscription_activated",
)


class WheelError(ValueError):
    """Raised for an invalid spin / redemption (surfaced to the client as a message)."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _now_iso() -> str:
    return _now().strftime(_TS_FMT)


def _ttl_expiry() -> str:
    return (_now() + datetime.timedelta(hours=max(1, config.WHEEL_OFFER_TTL_HOURS))).strftime(_TS_FMT)


def _is_expired(row) -> bool:
    exp = row["expires_at"] if row else None
    if not exp:
        return False
    try:
        return _now() > datetime.datetime.strptime(str(exp)[:19], _TS_FMT)
    except (ValueError, TypeError):
        return False  # unparseable → treat as not expired rather than nuke a valid offer


def _norm_email(email):
    return (email or "").strip().lower() if isinstance(email, str) and email.strip() else None


def _norm_session(session) -> str:
    """A client-supplied wheel_session must look like our token; otherwise mint one."""
    if isinstance(session, str) and 16 <= len(session) <= 64 and session.replace("-", "").replace("_", "").isalnum():
        return session
    return new_session()


def new_session() -> str:
    return secrets.token_urlsafe(18)


def _ug(row, key):
    """Row/dict-safe getter (users row may be a sqlite3.Row or a dict)."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        try:
            return row.get(key)  # type: ignore[union-attr]
        except AttributeError:
            return None


# --------------------------------------------------------------------------- #
# Prize selection — secure, weighted, backend-only
# --------------------------------------------------------------------------- #
def pick_prize() -> str:
    """Return a prize key chosen by secure weighted selection over
    config.WHEEL_WEIGHTS. Priority order (highest odds first) is preserved and the
    distribution is NEVER uniform. Uses secrets (CSPRNG), not random."""
    weights = config.WHEEL_WEIGHTS
    order = [p for p in config.WHEEL_PRIZE_ORDER if weights.get(p, 0) > 0]
    total = sum(weights[p] for p in order)
    if total <= 0:
        raise WheelError("Wheel is not configured.")
    r = secrets.randbelow(total)  # uniform in [0, total)
    acc = 0
    for p in order:
        acc += weights[p]
        if r < acc:
            return p
    return order[-1]  # unreachable; guards float/rounding paranoia


def prize_offer(prize: str) -> dict:
    """Display-ready offer for a prize: promo/regular prices + renewal terms."""
    cfg = config.wheel_prize_config(prize)
    interval = cfg["interval"]
    label = {"yearly": "year", "quarterly": "3 months", "monthly": "month"}[interval]
    regular = _usd(cfg["regular_cents"])
    promo = _usd(cfg["promo_cents"])
    renewal = {
        "yearly":    f"Then {regular}/year. Auto-renews yearly. Cancel anytime.",
        "quarterly": f"Then {regular} every 3 months. Auto-renews quarterly. Cancel anytime.",
        "monthly":   f"Then {regular}/month from month 2. Auto-renews monthly. Cancel anytime.",
    }[interval]
    return {
        "prize": prize,
        "interval": interval,
        "label": label,
        "promo_price": promo,
        "regular_price": regular,
        "promo_cents": cfg["promo_cents"],
        "regular_cents": cfg["regular_cents"],
        "renewal_terms": renewal,
        "has_trial": False,  # promotional checkouts never include a free trial
    }


def _usd(cents: int) -> str:
    whole = cents / 100.0
    return f"${int(whole)}" if float(whole).is_integer() else f"${whole:.2f}"


def public_config() -> dict:
    """Public, safe wheel config for /api/config: enabled flag, offer TTL, and the
    display prizes with their transparent odds (spec: show probabilities in the
    rules). No secrets; the actual prize is still decided server-side per spin."""
    total = sum(config.WHEEL_WEIGHTS.get(p, 0) for p in config.WHEEL_PRIZE_ORDER) or 1
    prizes = []
    for p in config.WHEEL_PRIZE_ORDER:
        w = config.WHEEL_WEIGHTS.get(p, 0)
        if w <= 0:
            continue
        offer = prize_offer(p)
        offer["weight"] = w
        offer["odds_pct"] = round(w / total * 100, 1)
        prizes.append(offer)
    return {
        "enabled": config.wheel_ready(),
        "ttl_hours": config.WHEEL_OFFER_TTL_HOURS,
        "prizes": prizes,
    }


# --------------------------------------------------------------------------- #
# Redemptions
# --------------------------------------------------------------------------- #
def _row_to_dict(r) -> dict | None:
    if not r:
        return None
    return {k: r[k] for k in r.keys()}


def get_redemption(redemption_id) -> dict | None:
    if not isinstance(redemption_id, str) or not redemption_id:
        return None
    with db.cursor() as c:
        r = c.execute(
            "SELECT * FROM promo_redemptions WHERE redemption_id = ?", (redemption_id,)
        ).fetchone()
    return _row_to_dict(r)


def _active_for_session(session, email, user_id) -> dict | None:
    """An existing pending, unexpired redemption for this session/email/user, so a
    re-spin returns the SAME prize instead of rerolling."""
    with db.cursor() as c:
        rows = c.execute(
            "SELECT * FROM promo_redemptions WHERE status = 'pending' AND ("
            "wheel_session = ? OR (email IS NOT NULL AND email = ?) OR (user_id IS NOT NULL AND user_id = ?)"
            ") ORDER BY created_at DESC",
            (session, _norm_email(email), user_id),
        ).fetchall()
    for r in rows:
        d = _row_to_dict(r)
        if not _is_expired(d):
            return d
    return None


def create_redemption(session, email=None, user_id=None) -> dict:
    """Pick a prize (weighted) and persist a pending redemption, OR return the
    session's existing active one. Returns an offer payload incl. redemption_id and
    the (possibly server-minted) wheel_session. No Stripe call here."""
    if not config.wheel_ready():
        raise WheelError("The Lucky Wheel is not available right now.")
    session = _norm_session(session)
    existing = _active_for_session(session, email, user_id)
    if existing:
        return _offer_payload(existing, session)

    prize = pick_prize()
    cfg = config.wheel_prize_config(prize)
    rid = secrets.token_urlsafe(24)
    with db.cursor() as c:
        c.execute(
            "INSERT INTO promo_redemptions (redemption_id, wheel_session, user_id, email, "
            "prize, promo_price_cents, regular_price_cents, status, expires_at) "
            "VALUES (?,?,?,?,?,?,?, 'pending', ?)",
            (rid, session, user_id, _norm_email(email), prize,
             cfg["promo_cents"], cfg["regular_cents"], _ttl_expiry()),
        )
    log_event("prize_selected", redemption_id=rid, wheel_session=session, prize=prize)
    row = get_redemption(rid)
    return _offer_payload(row, session)


def _offer_payload(row: dict, session: str = None) -> dict:
    offer = prize_offer(row["prize"])
    offer.update({
        "redemption_id": row["redemption_id"],
        "wheel_session": session or row.get("wheel_session"),
        "expires_at": row.get("expires_at"),
        "status": row.get("status"),
    })
    return offer


def _account_promo_eligible(user) -> bool:
    """A genuine NEW customer: free tier, never subscribed, no Stripe subscription on
    record. (Mirrors auth.starter_eligible's record checks; the promo is new-customer
    only, independent of the trial date gate.)"""
    import auth
    return (auth.tier(user) == "free"
            and not _ug(user, "subscribed_at")
            and not _ug(user, "stripe_subscription"))


def already_redeemed(user) -> bool:
    """True if this account/email already has a redeemed promo (one per customer)."""
    uid = _ug(user, "id")
    email = _norm_email(_ug(user, "email"))
    with db.cursor() as c:
        r = c.execute(
            "SELECT 1 FROM promo_redemptions WHERE status = 'redeemed' AND ("
            "(user_id IS NOT NULL AND user_id = ?) OR (email IS NOT NULL AND email = ?)) LIMIT 1",
            (uid, email),
        ).fetchone()
    return bool(r)


def validate_redemption(redemption_id, user) -> dict:
    """Re-validate a won offer at CHECKOUT and bind it to the account. Raises
    WheelError if it can't be applied. Returns the checkout promo descriptor
    {redemption_id, prize, interval, coupon, promo_cents, regular_cents}."""
    row = get_redemption(redemption_id)
    if not row:
        raise WheelError("This offer is no longer available.")
    if row["status"] == "redeemed":
        raise WheelError("This offer has already been redeemed.")
    if row["status"] != "pending":
        raise WheelError("This offer is no longer valid.")
    if _is_expired(row):
        _set_status(redemption_id, "expired")
        raise WheelError("This offer has expired.")
    if not _account_promo_eligible(user):
        raise WheelError("This promotional offer is for new customers only.")
    if already_redeemed(user):
        raise WheelError("You've already redeemed a promotional offer.")

    # Bind to this account so a second account/email can't reuse the same offer.
    _bind_user(redemption_id, user)
    cfg = config.wheel_prize_config(row["prize"])
    return {
        "redemption_id": redemption_id,
        "prize": row["prize"],
        "interval": cfg["interval"],
        "coupon": cfg["coupon"],
        "promo_cents": row["promo_price_cents"],
        "regular_cents": row["regular_price_cents"],
    }


def _bind_user(redemption_id, user) -> None:
    with db.cursor() as c:
        c.execute(
            "UPDATE promo_redemptions SET user_id = COALESCE(user_id, ?), "
            "email = COALESCE(email, ?) WHERE redemption_id = ?",
            (_ug(user, "id"), _norm_email(_ug(user, "email")), redemption_id),
        )


def _set_status(redemption_id, status) -> None:
    with db.cursor() as c:
        c.execute(
            "UPDATE promo_redemptions SET status = ? WHERE redemption_id = ?",
            (status, redemption_id),
        )


def mark_redeemed(redemption_id, user_id=None, customer=None, subscription=None,
                  session=None) -> bool:
    """Called from the Stripe webhook on a paid checkout carrying this redemption_id.
    Idempotent: only a still-pending row is transitioned to 'redeemed'."""
    if not redemption_id:
        return False
    with db.cursor() as c:
        cur = c.execute(
            "UPDATE promo_redemptions SET status = 'redeemed', redeemed_at = ?, "
            "user_id = COALESCE(?, user_id), stripe_subscription = COALESCE(?, stripe_subscription), "
            "stripe_session = COALESCE(?, stripe_session) "
            "WHERE redemption_id = ? AND status != 'redeemed'",
            (_now_iso(), user_id, subscription, session, redemption_id),
        )
        changed = cur.rowcount > 0
    if changed:
        log_event("subscription_activated", redemption_id=redemption_id)
    return changed


def mark_coupon(redemption_id, coupon_id) -> None:
    """Record the Stripe coupon id actually applied (bookkeeping)."""
    if not (redemption_id and coupon_id):
        return
    with db.cursor() as c:
        c.execute(
            "UPDATE promo_redemptions SET coupon_id = COALESCE(coupon_id, ?) WHERE redemption_id = ?",
            (coupon_id, redemption_id),
        )


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def log_event(event, redemption_id=None, wheel_session=None, prize=None, meta=None) -> None:
    """Append a funnel event. Best-effort — analytics must never break a request."""
    if not isinstance(event, str) or not event:
        return
    event = event[:48]
    mj = None
    if meta is not None:
        try:
            mj = json.dumps(meta)[:4000]
        except (TypeError, ValueError):
            mj = None
    try:
        with db.cursor() as c:
            c.execute(
                "INSERT INTO wheel_events (redemption_id, wheel_session, event, prize, meta_json) "
                "VALUES (?,?,?,?,?)",
                (redemption_id, wheel_session, event, prize, mj),
            )
    except Exception as e:  # noqa: BLE001
        print(f"[caloria][wheel] event log failed: {e}")


def conversion_summary() -> dict:
    """Wins vs purchases per prize + raw funnel-event counts (admin dashboard)."""
    out = {"prizes": {}, "events": {}, "weights": dict(config.WHEEL_WEIGHTS)}
    with db.cursor() as c:
        for r in c.execute(
            "SELECT prize, status, COUNT(*) n FROM promo_redemptions GROUP BY prize, status"
        ).fetchall():
            p = out["prizes"].setdefault(r["prize"], {"pending": 0, "redeemed": 0, "expired": 0})
            p[r["status"]] = r["n"]
        for r in c.execute(
            "SELECT event, COUNT(*) n FROM wheel_events GROUP BY event"
        ).fetchall():
            out["events"][r["event"]] = r["n"]
    # Per-prize conversion rate = redeemed / (wins = pending+redeemed+expired).
    for p, s in out["prizes"].items():
        wins = s.get("pending", 0) + s.get("redeemed", 0) + s.get("expired", 0)
        s["wins"] = wins
        s["conversion_rate"] = round(s.get("redeemed", 0) / wins, 4) if wins else 0.0
    return out
