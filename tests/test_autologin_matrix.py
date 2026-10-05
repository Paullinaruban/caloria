"""
Auto-login reset test matrix — runs against an ISOLATED temp DB (CALORIA_DB).
Proves the end-to-end contract the frontend patch now depends on:
reset_password_with_code() -> {token, user}, that token is a live session,
the NEW password logs in, and the OLD password no longer does.
"""
import os, sys, tempfile

# Make the backend modules (one dir up) importable no matter the CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Isolate: point the backend at a throwaway DB before importing anything.
tmp = tempfile.NamedTemporaryFile(prefix="caloria-test-", suffix=".db", delete=False)
tmp.close()
os.environ["CALORIA_DB"] = tmp.name

import db, auth, tokens
db.init_db()

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

EMAIL = "matrix.user@example.com"
OLD_PW = "OldPass123"
NEW_PW = "BrandNewPass456"

print("STEP 1 — signup")
u = auth.signup(EMAIL, OLD_PW, "Matrix User")
uid = u["user"]["id"] if isinstance(u, dict) and "user" in u else (u.get("id") if isinstance(u, dict) else None)
check("signup returns a user id", bool(uid))

print("STEP 2 — login with ORIGINAL password works")
try:
    s = auth.login(EMAIL, OLD_PW)
    check("original login returns token", bool(s.get("token")))
    orig_token = s["token"]
except Exception as e:
    check(f"original login works (got {e!r})", False); orig_token = None

print("STEP 3 — request_reset issues a reset code")
code = tokens.issue_code(uid, "reset", 30)
check("reset code issued (6 digits)", bool(code) and len(str(code)) >= 4)

print("STEP 4 — check_reset_code is non-burning and valid")
check("check_reset_code True", auth.check_reset_code(EMAIL, code) is True)
check("check_reset_code still True (non-burning)", auth.check_reset_code(EMAIL, code) is True)

print("STEP 5 — reset_password_with_code returns an auto-login payload")
res = auth.reset_password_with_code(EMAIL, code, NEW_PW)
check("reset returns dict (not True)", isinstance(res, dict))
check("reset payload has token", isinstance(res, dict) and bool(res.get("token")))
check("reset payload has user", isinstance(res, dict) and bool(res.get("user")))
new_token = res.get("token") if isinstance(res, dict) else None

print("STEP 6 — the returned token is a LIVE session for this user")
who = auth.user_for_token(new_token)
check("user_for_token resolves", who is not None)
check("resolved session is the right user", who is not None and who["id"] == uid)

print("STEP 7 — pre-reset sessions were invalidated")
if orig_token:
    check("old session token is now dead", auth.user_for_token(orig_token) is None)
else:
    check("old session token is now dead (n/a)", True)

print("STEP 8 — NEW password logs in, OLD password does NOT")
try:
    s2 = auth.login(EMAIL, NEW_PW)
    check("login with NEW password works", bool(s2.get("token")))
except Exception as e:
    check(f"login with NEW password works (got {e!r})", False)
try:
    auth.login(EMAIL, OLD_PW)
    check("login with OLD password rejected", False)
except auth.AuthError:
    check("login with OLD password rejected", True)
except Exception as e:
    check(f"login with OLD password rejected (unexpected {e!r})", False)

print("STEP 9 — reusing the consumed reset code fails")
res2 = auth.reset_password_with_code(EMAIL, code, "AnotherPass789")
check("consumed code cannot be reused", res2 is False)

print()
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
print("ALL AUTO-LOGIN MATRIX CHECKS PASSED")

os.unlink(tmp.name)
