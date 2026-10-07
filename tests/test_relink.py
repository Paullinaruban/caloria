"""Tests for admin.relink_subscription — moves an ACTIVE Stripe link between two
of a customer's own accounts, grants target premium, demotes source, with guards.
Mocked Stripe, isolated DB.

Run:  python3 tests/test_relink.py
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.NamedTemporaryFile(prefix="caloria-relink-", suffix=".db", delete=False); tmp.close()
os.environ["CALORIA_DB"] = tmp.name
import db, auth, billing, admin, config
db.init_db()
PASS, FAIL = [], []
def check(n, c): (PASS if c else FAIL).append(n); print(f"  [{'PASS' if c else 'FAIL'}] {n}")
config.stripe_ready = lambda: True

def stripe_status(status):
    def _s(path, method="GET", **kw): return {"status": status, "id": "sub_x"}
    return _s

def mkuser(email, *, plan="free", status="", customer=None, sub=None):
    with db.cursor() as c:
        c.execute("INSERT INTO users (email,name,pw_salt,pw_hash,plan,subscription_status,"
                  "stripe_customer,stripe_subscription,terms_accepted,privacy_accepted) "
                  "VALUES (?,?,?,?,?,?,?,?,1,1)",
                  (email,"T","s","h",plan,status,customer,sub))
        return c.lastrowid

print("TEST 1 — Theresa scenario: paid(premium+sub) -> gated(free) moves link, flips premium")
billing._stripe = stripe_status("active")
paid = mkuser("theresa.crook@icloud.com", plan="premium", status="active",
              customer="cus_V5Pya23zGCW7p2", sub="sub_1U5F8H")
gated = mkuser("lungful_tumulus4e@icloud.com", plan="free", status="")
res = admin.relink_subscription(paid, gated)
check("before: source premium, target not", res["before"]["from_is_premium"] and not res["before"]["to_is_premium"])
check("after: TARGET is premium", res["after"]["to_is_premium"] is True)
check("after: SOURCE back to free", res["after"]["from_is_premium"] is False and res["after"]["from_plan"] == "free")
check("target carries the SAME customer", res["after"]["to_stripe_customer"] == "cus_V5Pya23zGCW7p2")
check("target carries the SAME subscription", res["after"]["to_stripe_subscription"] == "sub_1U5F8H")
check("is_premium(target) True", auth.is_premium(auth._get_user(gated)) is True)
p = auth._get_user(paid)
check("source detached (no stripe fields)", p["stripe_customer"] is None and p["stripe_subscription"] is None)
check("source status = 'moved'", p["subscription_status"] == "moved")

print("TEST 2 — guard: refuse if Stripe does NOT report active")
billing._stripe = stripe_status("canceled")
a = mkuser("a@example.com", plan="premium", status="active", customer="cus_A", sub="sub_A")
b = mkuser("b@example.com")
try:
    admin.relink_subscription(a, b); check("should have refused", False)
except ValueError as e:
    check("refused: not active in Stripe", "active" in str(e).lower())
check("target untouched", auth.is_premium(auth._get_user(b)) is False)
check("source untouched (still premium)", auth.is_premium(auth._get_user(a)) is True)

print("TEST 3 — guard: refuse if source has no subscription")
billing._stripe = stripe_status("active")
c1 = mkuser("c1@example.com", plan="free")
c2 = mkuser("c2@example.com")
try:
    admin.relink_subscription(c1, c2); check("should have refused (no sub)", False)
except ValueError as e:
    check("refused: no customer/subscription", "no stripe" in str(e).lower())

print("TEST 4 — guard: refuse clobbering a DIFFERENT sub on the target")
billing._stripe = stripe_status("active")
d1 = mkuser("d1@example.com", plan="premium", status="active", customer="cus_D", sub="sub_D")
d2 = mkuser("d2@example.com", plan="premium", status="active", customer="cus_E", sub="sub_E")
try:
    admin.relink_subscription(d1, d2); check("should have refused (clobber)", False)
except ValueError as e:
    check("refused: target has different subscription", "different subscription" in str(e).lower())

print("TEST 5 — guard: refuse same account")
try:
    admin.relink_subscription(d1, d1); check("should have refused (same)", False)
except ValueError as e:
    check("refused: same account", "same" in str(e).lower())

print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL: print("FAILED:", FAIL); sys.exit(1)
print("ALL RELINK CHECKS PASSED")
os.unlink(tmp.name)
