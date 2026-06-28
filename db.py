"""SQLite storage for Caloria.

Tables:
  users        — accounts, plan, scan usage, profile, targets
  sessions     — bearer tokens
  meals        — server-side meal history per user
  usda_cache   — cached USDA per-100g lookups
  corrections  — user portion corrections (learning loop)
  kv           — small key/value store (e.g. auto-created Stripe price ids)

For production scale, swap the connection factory for Postgres — the queries
are standard SQL.
"""
import sqlite3
import threading
from contextlib import contextmanager

import config

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def cursor():
    with _lock:
        conn = _connect()
        try:
            yield conn.cursor()
            conn.commit()
        finally:
            conn.close()


def _add_column(c, table, col, decl):
    cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    with cursor() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT UNIQUE NOT NULL,
                name          TEXT,
                pw_salt       TEXT NOT NULL,
                pw_hash       TEXT NOT NULL,
                plan          TEXT NOT NULL DEFAULT 'free',
                scans_used    INTEGER NOT NULL DEFAULT 0,
                stripe_customer TEXT,
                profile_json  TEXT,
                targets_json  TEXT,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS meals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT,
                image       TEXT,
                data_json   TEXT NOT NULL,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_meals_user ON meals(user_id)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS usda_cache (
                query        TEXT PRIMARY KEY,
                fdc_id       INTEGER,
                description  TEXT,
                data_type    TEXT,
                kcal_100g    REAL,
                protein_100g REAL,
                carbs_100g   REAL,
                fat_100g     REAL,
                fiber_100g   REAL,
                sugar_100g   REAL,
                sodium_100g  REAL,
                fetched_at   TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # Migrate older caches that predate sugar/sodium.
        _add_column(c, "usda_cache", "sugar_100g", "REAL")
        _add_column(c, "usda_cache", "sodium_100g", "REAL")
        c.execute(
            """CREATE TABLE IF NOT EXISTS corrections (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ingredient       TEXT NOT NULL,
                predicted_grams  REAL NOT NULL,
                corrected_grams  REAL NOT NULL,
                created_at       TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_corr_ingredient ON corrections(ingredient)")
        c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")

        # --- launch hardening: email verification, billing status, sessions ---
        _add_column(c, "users", "email_verified", "INTEGER NOT NULL DEFAULT 0")
        _add_column(c, "users", "subscription_status", "TEXT")     # active|trialing|past_due|canceled
        _add_column(c, "users", "stripe_subscription", "TEXT")     # sub_...
        _add_column(c, "users", "active", "INTEGER NOT NULL DEFAULT 1")  # admin can deactivate
        # Consent evidence captured at signup (auditable).
        _add_column(c, "users", "terms_accepted", "INTEGER NOT NULL DEFAULT 0")
        _add_column(c, "users", "privacy_accepted", "INTEGER NOT NULL DEFAULT 0")
        _add_column(c, "users", "policy_version", "TEXT")
        _add_column(c, "users", "policy_accepted_at", "TEXT")
        # Billing event log — powers subscription history & failed-payment views.
        c.execute(
            """CREATE TABLE IF NOT EXISTS billing_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                customer   TEXT,
                type       TEXT,
                status     TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_billing_customer ON billing_events(customer)")
        # Secure, single-use, time-limited tokens for email verify + password reset.
        c.execute(
            """CREATE TABLE IF NOT EXISTS account_tokens (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                kind       TEXT NOT NULL,        -- 'verify' | 'reset'
                token_hash TEXT NOT NULL,        -- sha256(raw token); raw never stored
                expires_at TEXT NOT NULL,        -- ISO-8601 UTC
                used_at    TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_token_hash ON account_tokens(token_hash)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_token_user ON account_tokens(user_id, kind)")
        # Failed-attempt counter for 6-digit verification codes (brute-force guard).
        _add_column(c, "account_tokens", "attempts", "INTEGER NOT NULL DEFAULT 0")
        # Achievement unlocks — records the first time a user earns each badge,
        # so we can show unlock dates and surface "new badge" celebrations.
        c.execute(
            """CREATE TABLE IF NOT EXISTS achievements (
                user_id     INTEGER NOT NULL,
                key         TEXT NOT NULL,
                unlocked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, key)
            )"""
        )
        # Daily Ritual — one row per user per day (morning check-in + evening reflection).
        c.execute(
            """CREATE TABLE IF NOT EXISTS daily_rituals (
                user_id    INTEGER NOT NULL,
                date       TEXT NOT NULL,
                energy     TEXT, mood TEXT, sleep TEXT, hydration TEXT,
                reflection TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, date)
            )"""
        )
        # Streak freezes — a "freeze" protects one missed day (1 free per week).
        c.execute(
            """CREATE TABLE IF NOT EXISTS streak_freezes (
                user_id    INTEGER NOT NULL,
                date       TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, date)
            )"""
        )
        # In-app notification log — enforces per-key cooldowns + seen-state so
        # the same message doesn't repeat daily and the badge isn't always on.
        c.execute(
            """CREATE TABLE IF NOT EXISTS notification_log (
                user_id    INTEGER NOT NULL,
                key        TEXT NOT NULL,
                last_shown TEXT,
                PRIMARY KEY (user_id, key)
            )"""
        )
        # Retention-email log — dedupes sends (one per user/type/period).
        c.execute(
            """CREATE TABLE IF NOT EXISTS email_log (
                user_id    INTEGER NOT NULL,
                type       TEXT NOT NULL,
                period     TEXT NOT NULL,
                sent_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, type, period)
            )"""
        )


def kv_get(key: str):
    with cursor() as c:
        row = c.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else None


def kv_set(key: str, value: str):
    with cursor() as c:
        c.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, value))
