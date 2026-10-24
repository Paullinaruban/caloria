"""H1 regression: password reset must NOT hand a session to a deactivated/banned
account, while still auto-logging-in a normal active account. Isolated DB, email
send stubbed (no network).

Run:  python3 tests/test_reset_deactivated.py
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.NamedTemporaryFile(prefix="caloria-h1-", suffix=".db", delete=False); tmp.close()
os.environ["CALORIA_DB"] = tmp.name
import db, auth, tokens, email_send
db.init_db()
email_send._send = lambda *a, **k: None  # no outbound email

PASS, FAIL = [], []
def check(n, c): (PASS if c else FAIL).append(n); print(f"  [{'PASS' if c else 'FAIL'}] {n}")

def do_reset(email, newpw):
    row = auth._get_user_by_email(email)
    code = tokens.issue_code(row["id"], "reset", 30)
    return auth.reset_password_with_code(email, code, newpw)

def sessions_for(uid):
    with db.cursor() as c:
        return c.execute("SELECT COUNT(*) n FROM sessions WHERE user_id = ?", (uid,)).fetchone()["n"]

print("TEST 1 — ACTIVE account: reset still auto-logs in (unchanged behavior)")
uid = auth.signup("active@example.com", "OldPass123", "A")["user"]["id"]
res = do_reset("active@example.com", "NewPass456")
check("active reset returns an auth payload (dict)", isinstance(res, dict))
check("payload carries a token", isinstance(res, dict) and bool(res.get("token")))
check("token resolves to the same user",
      isinstance(res, dict) and auth.user_for_token(res["token"]) is not None
      and auth.user_for_token(res["token"])["id"] == uid)
try:
    s = auth.login("active@example.com", "NewPass456")
    check("active can log in with the new password", bool(s.get("token")))
except Exception as e:
    check(f"active can log in with the new password (got {e!r})", False)

print("TEST 2 — DEACTIVATED account: reset succeeds but issues NO session")
uid2 = auth.signup("banned@example.com", "OldPass123", "B")["user"]["id"]
with db.cursor() as c:
    c.execute("UPDATE users SET active = 0 WHERE id = ?", (uid2,))
res2 = do_reset("banned@example.com", "NewPass789")
check("deactivated reset returns True (success, NOT a token payload)", res2 is True)
check("no session row was created for the deactivated user", sessions_for(uid2) == 0)
try:
    auth.login("banned@example.com", "NewPass789")
    check("login with NEW password is denied for deactivated account", False)
except auth.AuthError as e:
    check("login with NEW password -> 403 deactivated (pw correct, access denied)", e.code == 403)
try:
    auth.login("banned@example.com", "OldPass123")
    check("login with OLD password is rejected", False)
except auth.AuthError as e:
    check("login with OLD password -> 401 (password really was changed)", e.code == 401)

print("TEST 3 — reactivating the account restores normal reset auto-login")
with db.cursor() as c:
    c.execute("UPDATE users SET active = 1 WHERE id = ?", (uid2,))
res3 = do_reset("banned@example.com", "NewPass999")
check("reactivated account auto-logs in again (dict+token)", isinstance(res3, dict) and bool(res3.get("token")))

print()
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL: print("FAILED:", FAIL); sys.exit(1)
print("ALL H1 RESET-DEACTIVATION CHECKS PASSED")
os.unlink(tmp.name)
