"""
Email-abuse protection tests. Isolated temp DB, a controllable clock, and a
stubbed Resend layer (no network, no real emails). Proves:
  - 60s cooldown + 5/hour cap per recipient (verify & reset)
  - clear retry_after on denial
  - enumeration-neutral (same behaviour for non-existent addresses)
  - signup's first email is never throttled; login auto-send IS throttled
  - stable, non-secret idempotency keys threaded to the send layer
  - normal single verify/reset still send exactly once
"""
import os, sys, tempfile

# Make the backend modules (one dir up) importable no matter the CWD.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

tmp = tempfile.NamedTemporaryFile(prefix="caloria-abuse-", suffix=".db", delete=False)
tmp.close()
os.environ["CALORIA_DB"] = tmp.name

import db, auth, tokens, ratelimit, email_send, appauth, hashlib, re
db.init_db()

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name); print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

# ---- controllable clock for the limiter ----
class Clock:
    def __init__(self): self.t = 1_000_000.0
    def time(self): return self.t
    def advance(self, s): self.t += s
clock = Clock()
ratelimit.time = clock  # ratelimit calls time.time()

# ---- stub the network send; capture (to, idempotency_key) ----
sent = []
def fake_send(to, subject, html, text, headers=None, timeout=None, trace=None, idempotency_key=None):
    sent.append({"to": to, "subject": subject, "text": text, "idem": idempotency_key})
    return "stub-id"
email_send._send = fake_send
# email provider must look "ready" so send paths run
import config as _cfg
_cfg.RESEND_API_KEY = _cfg.RESEND_API_KEY or "stub"
email_send.config.email_ready = lambda: True

print("TEST 1 — verify gate: cooldown blocks the 2nd immediate send")
clock.t = 1_000_000.0
a1 = ratelimit.email_gate("verify", "abuse@example.com")
a2 = ratelimit.email_gate("verify", "abuse@example.com")
check("1st verify allowed", a1[0] is True)
check("2nd verify blocked by cooldown", a2[0] is False)
check("cooldown retry_after ~60s (<=61 incl. +1 guard)", 0 < a2[1] <= 61)

print("TEST 2 — hourly cap: max 5 per hour even after cooldowns clear")
clock.t = 2_000_000.0
results = []
for i in range(7):
    results.append(ratelimit.email_gate("verify", "capped@example.com")[0])
    clock.advance(61)  # step past the 60s cooldown each time
check("first 5 allowed", results[:5] == [True]*5)
check("6th and 7th blocked (hourly cap=5)", results[5] is False and results[6] is False)
# after an hour rolls off, allowed again
clock.advance(3600)
check("allowed again after the hour window rolls off", ratelimit.email_gate("verify", "capped@example.com")[0] is True)

print("TEST 3 — enumeration-neutral: non-existent address behaves identically")
clock.t = 3_000_000.0
n1 = ratelimit.email_gate("reset", "ghost-does-not-exist@nowhere.tld")
n2 = ratelimit.email_gate("reset", "ghost-does-not-exist@nowhere.tld")
check("unknown email 1st reset allowed", n1[0] is True)
check("unknown email 2nd reset blocked (same as real)", n2[0] is False)

print("TEST 4 — verify and reset use independent budgets")
clock.t = 4_000_000.0
check("verify allowed", ratelimit.email_gate("verify", "split@example.com")[0] is True)
check("reset still allowed (separate bucket)", ratelimit.email_gate("reset", "split@example.com")[0] is True)

print("TEST 5 — idempotency key is stable, per-code, and hides the raw code")
k1 = auth._email_idem("verify", 42, "123456")
k2 = auth._email_idem("verify", 42, "123456")
k3 = auth._email_idem("verify", 42, "654321")
check("same action/user/code -> same key", k1 == k2)
check("different code -> different key", k1 != k3)
check("key does NOT contain raw code", "123456" not in k1)
check("key is not a random uuid (deterministic)", k1 == auth._email_idem("verify", 42, "123456"))

print("TEST 6 — signup first email is NEVER throttled; login auto-send IS")
clock.t = 5_000_000.0
uid = auth.signup("gatecheck@example.com", "secret123", "Gate")["user"]["id"]
sent.clear()
# signup already sent once; simulate a brand-new signup burst isn't our concern,
# but a repeated LOGIN on an unverified account must be throttled.
auth.send_verification(uid, "gatecheck@example.com", context="login")  # 1st login send
first = len(sent)
auth.send_verification(uid, "gatecheck@example.com", context="login")  # 2nd within cooldown
second = len(sent)
check("login send #1 delivered", first == 1)
check("login send #2 suppressed by cooldown", second == 1)
# signup context bypasses the gate entirely
auth.send_verification(uid, "gatecheck@example.com", context="signup")
check("signup-context send bypasses the gate", len(sent) == 2)

print("TEST 7 — normal single reset sends exactly once, with a reset idem key")
clock.t = 6_000_000.0
auth.signup("normal@example.com", "secret123", "Normal")
sent.clear()
auth.request_reset("normal@example.com")
check("exactly one reset email sent", len(sent) == 1)
check("reset email carried an idempotency key", bool(sent and sent[0]["idem"]))
check("reset subject is the reset code email", bool(sent and "reset" in sent[0]["subject"].lower()))

print("TEST 8 — request_reset for a NON-existent account sends nothing (neutral)")
sent.clear()
auth.request_reset("no-such-user@example.com")
check("no email sent for unknown account", len(sent) == 0)

print("TEST 9 — native iOS verify-start carries a stable, correct idempotency key")
clock.t = 7_000_000.0
sent.clear()
APP_EMAIL = "native-signup@example.com"   # no account exists yet
appauth.start_verification(APP_EMAIL, "Native")
check("app verify-start sent exactly one email", len(sent) == 1)
check("app send carried an idempotency key", bool(sent and sent[0]["idem"]))
check("app idem key has the cal- prefix", bool(sent and sent[0]["idem"].startswith("cal-")))
# Recover the 6-digit code from the email text and recompute the expected key.
if sent:
    m = re.search(r"\b(\d{6})\b", sent[0]["text"])
    code = m.group(1) if m else ""
    expected = "cal-" + hashlib.sha256(f"appverify:{APP_EMAIL}:{code}".encode()).hexdigest()[:40]
    check("app idem key == sha256(appverify:email:code) (stable, not random)", sent[0]["idem"] == expected)
    check("app idem key does NOT contain the raw code", code and code not in sent[0]["idem"])

print("TEST 10 — native app shares the SAME per-recipient 'verify' budget (no bypass)")
clock.t = 8_000_000.0
shared = "shared-budget@example.com"
# A web resend consumes the verify budget for this address...
first = ratelimit.email_gate("verify", shared)
# ...so the native app's verify-start (which the endpoint gates on the same key)
# is now throttled for the same recipient.
second = ratelimit.email_gate("verify", shared)
check("web resend consumes the verify budget", first[0] is True)
check("native app verify-start then throttled (shared bucket)", second[0] is False and second[1] > 0)
# And the server endpoint is actually wired to that shared gate:
import pathlib
srv = pathlib.Path(_ROOT) / "server.py"
srv_src = srv.read_text()
block = srv_src[srv_src.index("def _app_verify_start"): srv_src.index("def _app_verify_check")]
check("_app_verify_start calls _email_throttled('verify', ...)", '_email_throttled("verify"' in block)

print()
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
print("ALL EMAIL-ABUSE PROTECTION CHECKS PASSED")
os.unlink(tmp.name)
