"""Proves admin.entitlement_trace() reports the correct decision chain, live
Stripe verdict, and (with fix=True) self-heals a stale row. Mocked Stripe.

Run:  python3 tests/test_entitlement_trace.py
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.NamedTemporaryFile(prefix="caloria-trace-", suffix=".db", delete=False); tmp.close()
os.environ["CALORIA_DB"] = tmp.name
import db, auth, billing, admin, config
db.init_db()

PASS, FAIL = [], []
def check(n, c): (PASS if c else FAIL).append(n); print(f"  [{'PASS' if c else 'FAIL'}] {n}")
config.stripe_ready = lambda: True

def stripe_returning(status, **extra):
    def _s(path, method="GET", **kw): return {"status": status, "id": "sub_x", **extra}
    return _s

def mkuser(email, *, name="T", plan="free", status="", customer=None, sub=None, iap=0):
    with db.cursor() as c:
        c.execute("INSERT INTO users (email,name,pw_salt,pw_hash,plan,subscription_status,"
                  "stripe_customer,stripe_subscription,iap_active,terms_accepted,privacy_accepted) "
                  "VALUES (?,?,?,?,?,?,?,?,?,1,1)",
                  (email, name, "s", "h", plan, status, customer, sub, iap))

print("TEST A — stale cache (Stripe ACTIVE, row says free) -> STALE CACHE verdict, fix heals")
billing._stripe = stripe_returning("active", current_period_end=9999999999, cancel_at_period_end=False)
mkuser("stale@example.com", plan="free", status="", customer="cus_1", sub="sub_1")
t = admin.entitlement_trace("stale@example.com")
check("is_premium False in chain", t["decision_chain"]["is_premium"] is False)
check("stripe_says_active True", t["stripe_live"]["stripe_says_active"] is True)
check("verdict = STALE CACHE", t["verdict"].startswith("STALE CACHE"))
t2 = admin.entitlement_trace("stale@example.com", fix=True)
check("fix reconciled", t2["fix"]["reconciled"] is True)
check("is_premium_after True", t2["fix"]["is_premium_after"] is True)
check("plan_after premium", t2["fix"]["plan_after"] == "premium")

print("TEST B — genuine lapse (Stripe CANCELED) -> GENUINE LAPSE verdict, no heal")
billing._stripe = stripe_returning("canceled")
mkuser("lapsed@example.com", plan="free", status="", customer="cus_2", sub="sub_2")
t = admin.entitlement_trace("lapsed@example.com", fix=True)
check("stripe_says_active False", t["stripe_live"]["stripe_says_active"] is False)
check("verdict = GENUINE LAPSE", t["verdict"].startswith("GENUINE LAPSE"))
check("fix did NOT reconcile", t["fix"]["reconciled"] is False)
check("still not premium", t["fix"]["is_premium_after"] is False)

print("TEST C — wrong account (no sub on row, sibling shares name) -> WRONG ACCOUNT verdict")
mkuser("theresa@example.com", name="Theresa Crook", plan="free", status="", customer=None, sub=None)
mkuser("theresa.old@example.com", name="Theresa Crook", plan="premium", status="active", customer="cus_T", sub="sub_T")
billing._stripe = stripe_returning("active")
t = admin.entitlement_trace("theresa@example.com")
check("no stripe on this row", t["user"]["stripe_subscription"] is None)
check("duplicate detected", len(t["duplicate_accounts"]) >= 1)
check("verdict = POSSIBLE WRONG ACCOUNT", t["verdict"].startswith("POSSIBLE WRONG ACCOUNT"))

print("TEST D — already premium -> ENTITLED verdict")
mkuser("good@example.com", plan="premium", status="active", customer="cus_G", sub="sub_G")
billing._stripe = stripe_returning("active")
t = admin.entitlement_trace("good@example.com")
check("is_premium True", t["decision_chain"]["is_premium"] is True)
check("verdict = ENTITLED", t["verdict"].startswith("ENTITLED"))

print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL: print("FAILED:", FAIL); sys.exit(1)
print("ALL ENTITLEMENT-TRACE CHECKS PASSED")
os.unlink(tmp.name)
