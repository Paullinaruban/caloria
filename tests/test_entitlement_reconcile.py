"""
Self-heal entitlement reconcile tests. Isolated temp DB, mocked Stripe (no
network). Proves an account wrongly cached as non-premium is recovered IFF
Stripe's live status says active, that it never downgrades or masks a real
cancellation, and that it doesn't hammer Stripe for free / terminal rows.

Run:  python3 tests/test_entitlement_reconcile.py
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.NamedTemporaryFile(prefix="caloria-recon-", suffix=".db", delete=False)
tmp.close()
os.environ["CALORIA_DB"] = tmp.name

import db, auth, billing, config
db.init_db()

PASS, FAIL = [], []
def check(n, c): (PASS if c else FAIL).append(n); print(f"  [{'PASS' if c else 'FAIL'}] {n}")

config.stripe_ready = lambda: True  # pretend Stripe is configured

calls = {"n": 0}
def make_stripe(status):
    def _s(path, method="GET", **kw):
        calls["n"] += 1
        return {"status": status, "id": "sub_x"}
    return _s

def mkuser(email, *, plan="free", status="", customer=None, sub=None, iap=0):
    with db.cursor() as c:
        c.execute("INSERT INTO users (email, name, pw_salt, pw_hash, plan, subscription_status, "
                  "stripe_customer, stripe_subscription, iap_active, terms_accepted, privacy_accepted) "
                  "VALUES (?,?,?,?,?,?,?,?,?,1,1)",
                  (email, "T", "s", "h", plan, status, customer, sub, iap))
        uid = c.lastrowid
    return auth._get_user(uid)

print("TEST 1 — stale 'free' row, Stripe says ACTIVE -> recovered + row repaired")
calls["n"] = 0
billing._stripe = make_stripe("active")
u = mkuser("stale@example.com", plan="free", status="", customer="cus_1", sub="sub_1")
check("is_premium False before (stale cache)", auth.is_premium(u) is False)
check("reconcile returns True", billing.reconcile_entitlement(u) is True)
check("Stripe was consulted", calls["n"] == 1)
u2 = auth._get_user(u["id"])
check("row repaired to premium", u2["plan"] == "premium" and u2["subscription_status"] == "active")
check("is_premium True after reconcile", auth.is_premium(u2) is True)

print("TEST 2 — terminal 'canceled' status -> trusted, Stripe NOT called, stays gated")
calls["n"] = 0
billing._stripe = make_stripe("active")
u = mkuser("canceled@example.com", plan="free", status="canceled", customer="cus_2", sub="sub_2")
check("reconcile False for terminal status", billing.reconcile_entitlement(u) is False)
check("Stripe NOT consulted for a terminal status", calls["n"] == 0)
check("row unchanged (still free)", auth._get_user(u["id"])["plan"] == "free")

print("TEST 3 — Stripe says CANCELED -> not recovered, no false grant, no downgrade")
calls["n"] = 0
billing._stripe = make_stripe("canceled")
u = mkuser("reallylapsed@example.com", plan="free", status="paused", customer="cus_3", sub="sub_3")
check("reconcile False when Stripe says canceled", billing.reconcile_entitlement(u) is False)
check("Stripe was consulted", calls["n"] == 1)
check("row stays free (genuine lapse not masked)", auth._get_user(u["id"])["plan"] == "free")

print("TEST 4 — no Stripe subscription id -> no API call, no change")
calls["n"] = 0
billing._stripe = make_stripe("active")
u = mkuser("nosub@example.com", plan="free", status="", customer=None, sub=None)
check("reconcile False with no subscription id", billing.reconcile_entitlement(u) is False)
check("Stripe NOT consulted (free user)", calls["n"] == 0)

print("TEST 5 — Stripe API error -> graceful False, row untouched")
def boom(path, method="GET", **kw):
    raise billing.BillingError("stripe down")
billing._stripe = boom
u = mkuser("apierr@example.com", plan="free", status="", customer="cus_5", sub="sub_5")
check("reconcile False on Stripe error", billing.reconcile_entitlement(u) is False)
check("row unchanged on error", auth._get_user(u["id"])["plan"] == "free")

print("TEST 6 — already-premium user is never downgraded by any path")
billing._stripe = make_stripe("canceled")
u = mkuser("premium@example.com", plan="premium", status="active", customer="cus_6", sub="sub_6")
check("is_premium True (fast path)", auth.is_premium(u) is True)
billing.reconcile_entitlement(u)
check("row still premium after a reconcile call", auth._get_user(u["id"])["plan"] == "premium")

print("TEST 7 — trialing / past_due count as active (grace), via live check")
for st in ("trialing", "past_due"):
    billing._stripe = make_stripe(st)
    u = mkuser(f"{st}@example.com", plan="free", status="", customer=f"cus_{st}", sub=f"sub_{st}")
    check(f"Stripe '{st}' recovers access", billing.reconcile_entitlement(u) is True)

print()
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL: print("FAILED:", FAIL); sys.exit(1)
print("ALL ENTITLEMENT-RECONCILE CHECKS PASSED")
os.unlink(tmp.name)
