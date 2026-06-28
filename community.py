"""Supermodel Wellness Club — members community (stdlib + SQLite).

Profiles, posts (wins / food / workout / progress / transformation), likes,
comments, follows, a gamified level system, and a Success Wall (transformation
posts). No external services. Tables are created lazily so db.py is untouched.
"""
from __future__ import annotations

import json

import db

# (min_points, level_number, name)
LEVELS = [
    (0, 1, "New Member"),
    (50, 2, "Glow-Up Girl"),
    (150, 3, "Model In Progress"),
    (350, 4, "Runway Ready"),
    (700, 5, "Supermodel Status"),
]
_AVATARS = ["🌸", "💎", "👑", "🦋", "🌺", "✨", "🪩", "🌷", "💫", "🕊️"]
POST_TYPES = {"win", "food", "workout", "progress", "transformation"}
# Marketing base so social proof reads aspirational from day one.
_SOCIAL_BASE = 12400


def _ensure():
    with db.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS member_profiles (
            user_id INTEGER PRIMARY KEY, username TEXT, bio TEXT, avatar TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        # avatar_img holds an uploaded profile photo (data URL); avatar is the emoji fallback.
        cols = [r["name"] for r in c.execute("PRAGMA table_info(member_profiles)")]
        if "avatar_img" not in cols:
            c.execute("ALTER TABLE member_profiles ADD COLUMN avatar_img TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'win', text TEXT, image TEXT, image2 TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(id DESC)")
        c.execute("""CREATE TABLE IF NOT EXISTS post_likes (
            post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            PRIMARY KEY (post_id, user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS post_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, text TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS follows (
            follower_id INTEGER NOT NULL, followee_id INTEGER NOT NULL,
            PRIMARY KEY (follower_id, followee_id))""")


# ---------- profiles ----------
def ensure_profile(user_row) -> None:
    _ensure()
    uid = user_row["id"]
    with db.cursor() as c:
        row = c.execute("SELECT 1 FROM member_profiles WHERE user_id = ?", (uid,)).fetchone()
        if row:
            return
        base = (user_row["name"] or (user_row["email"] or "member").split("@")[0]).strip()
        username = "".join(ch for ch in base if ch.isalnum() or ch in " _-").strip()[:24] or f"member{uid}"
        c.execute(
            "INSERT INTO member_profiles (user_id, username, bio, avatar) VALUES (?,?,?,?)",
            (uid, username, "Building my supermodel era 💫", _AVATARS[uid % len(_AVATARS)]),
        )


def _level(points: int) -> dict:
    cur = LEVELS[0]
    for lv in LEVELS:
        if points >= lv[0]:
            cur = lv
    idx = LEVELS.index(cur)
    nxt = LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None
    if nxt:
        span = nxt[0] - cur[0]
        pct = round(min(100, (points - cur[0]) / span * 100)) if span else 100
    else:
        pct = 100
    return {"level": cur[1], "name": cur[2], "points": points,
            "next_at": nxt[0] if nxt else None, "progress": pct}


def _points(c, uid: int) -> int:
    """Compute points using an ALREADY-OPEN cursor (avoids re-locking)."""
    posts = c.execute("SELECT COUNT(*) n FROM posts WHERE user_id=?", (uid,)).fetchone()["n"]
    transf = c.execute("SELECT COUNT(*) n FROM posts WHERE user_id=? AND type='transformation'", (uid,)).fetchone()["n"]
    likes = c.execute("SELECT COUNT(*) n FROM post_likes l JOIN posts p ON p.id=l.post_id WHERE p.user_id=?", (uid,)).fetchone()["n"]
    comments = c.execute("SELECT COUNT(*) n FROM post_comments WHERE user_id=?", (uid,)).fetchone()["n"]
    return posts * 10 + transf * 20 + likes * 3 + comments * 2


def points_for(uid: int) -> int:
    with db.cursor() as c:
        return _points(c, uid)


def post_count(uid: int) -> int:
    """How many community posts this user has created (0 if none)."""
    _ensure()
    with db.cursor() as c:
        return c.execute("SELECT COUNT(*) n FROM posts WHERE user_id=?", (uid,)).fetchone()["n"]


def get_profile(uid: int, viewer_id=None) -> dict:
    _ensure()
    with db.cursor() as c:
        p = c.execute("SELECT mp.*, u.name FROM member_profiles mp JOIN users u ON u.id=mp.user_id WHERE mp.user_id=?", (uid,)).fetchone()
        if not p:
            return None
        followers = c.execute("SELECT COUNT(*) n FROM follows WHERE followee_id=?", (uid,)).fetchone()["n"]
        following = c.execute("SELECT COUNT(*) n FROM follows WHERE follower_id=?", (uid,)).fetchone()["n"]
        post_n = c.execute("SELECT COUNT(*) n FROM posts WHERE user_id=?", (uid,)).fetchone()["n"]
        is_following = False
        if viewer_id and viewer_id != uid:
            is_following = bool(c.execute("SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?", (viewer_id, uid)).fetchone())
        pts = _points(c, uid)
    return {
        "user_id": uid, "username": p["username"], "bio": p["bio"], "avatar": p["avatar"],
        "avatar_img": p["avatar_img"],
        "stats": {"posts": post_n, "followers": followers, "following": following},
        "level": _level(pts), "is_following": is_following, "is_me": viewer_id == uid,
    }


def update_profile(uid: int, username=None, bio=None, avatar=None, avatar_img=None) -> dict:
    _ensure()
    with db.cursor() as c:
        if username is not None:
            c.execute("UPDATE member_profiles SET username=? WHERE user_id=?", (str(username).strip()[:24] or "member", uid))
        if bio is not None:
            c.execute("UPDATE member_profiles SET bio=? WHERE user_id=?", (str(bio).strip()[:200], uid))
        if avatar is not None and str(avatar).strip():
            c.execute("UPDATE member_profiles SET avatar=? WHERE user_id=?", (str(avatar).strip()[:8], uid))
        if avatar_img is not None:
            img = str(avatar_img) if str(avatar_img).startswith("data:image") else None
            c.execute("UPDATE member_profiles SET avatar_img=? WHERE user_id=?", (img, uid))
    return get_profile(uid, uid)


# ---------- posts / feed ----------
def create_post(uid: int, ptype: str, text: str, image=None, image2=None) -> int:
    _ensure()
    ptype = ptype if ptype in POST_TYPES else "win"
    with db.cursor() as c:
        c.execute(
            "INSERT INTO posts (user_id, type, text, image, image2) VALUES (?,?,?,?,?)",
            (uid, ptype, str(text or "").strip()[:600], image, image2),
        )
        return c.lastrowid


def _post_dict(r, viewer_id, c) -> dict:
    likes = c.execute("SELECT COUNT(*) n FROM post_likes WHERE post_id=?", (r["id"],)).fetchone()["n"]
    liked = bool(c.execute("SELECT 1 FROM post_likes WHERE post_id=? AND user_id=?", (r["id"], viewer_id)).fetchone())
    ncom = c.execute("SELECT COUNT(*) n FROM post_comments WHERE post_id=?", (r["id"],)).fetchone()["n"]
    lvl = _level(_points(c, r["user_id"]))
    return {
        "id": r["id"], "type": r["type"], "text": r["text"], "image": r["image"], "image2": r["image2"],
        "created_at": r["created_at"], "author_id": r["user_id"],
        "username": r["username"], "avatar": r["avatar"], "avatar_img": r["avatar_img"],
        "level": lvl["level"], "level_name": lvl["name"],
        "likes": likes, "liked": liked, "comments": ncom,
    }


def feed(viewer_id: int, ptype=None, author_id=None, limit=60) -> list:
    _ensure()
    q = ("SELECT p.*, mp.username, mp.avatar, mp.avatar_img FROM posts p "
         "JOIN member_profiles mp ON mp.user_id=p.user_id WHERE 1=1")
    args = []
    if ptype:
        q += " AND p.type=?"; args.append(ptype)
    if author_id:
        q += " AND p.user_id=?"; args.append(author_id)
    q += " ORDER BY p.id DESC LIMIT ?"; args.append(limit)
    with db.cursor() as c:
        rows = c.execute(q, tuple(args)).fetchall()
        return [_post_dict(r, viewer_id, c) for r in rows]


def toggle_like(uid: int, post_id: int) -> dict:
    _ensure()
    with db.cursor() as c:
        ex = c.execute("SELECT 1 FROM post_likes WHERE post_id=? AND user_id=?", (post_id, uid)).fetchone()
        if ex:
            c.execute("DELETE FROM post_likes WHERE post_id=? AND user_id=?", (post_id, uid))
            liked = False
        else:
            c.execute("INSERT OR IGNORE INTO post_likes (post_id, user_id) VALUES (?,?)", (post_id, uid))
            liked = True
        n = c.execute("SELECT COUNT(*) n FROM post_likes WHERE post_id=?", (post_id,)).fetchone()["n"]
    return {"liked": liked, "likes": n}


def add_comment(uid: int, post_id: int, text: str) -> None:
    _ensure()
    t = str(text or "").strip()[:400]
    if not t:
        return
    with db.cursor() as c:
        c.execute("INSERT INTO post_comments (post_id, user_id, text) VALUES (?,?,?)", (post_id, uid, t))


def comments(post_id: int) -> list:
    _ensure()
    with db.cursor() as c:
        rows = c.execute(
            "SELECT cm.text, cm.created_at, mp.username, mp.avatar, mp.avatar_img FROM post_comments cm "
            "JOIN member_profiles mp ON mp.user_id=cm.user_id WHERE cm.post_id=? ORDER BY cm.id ASC LIMIT 100",
            (post_id,),
        ).fetchall()
    return [{"text": r["text"], "created_at": r["created_at"], "username": r["username"],
             "avatar": r["avatar"], "avatar_img": r["avatar_img"]} for r in rows]


def toggle_follow(uid: int, target: int) -> dict:
    _ensure()
    if uid == target:
        return {"following": False}
    with db.cursor() as c:
        ex = c.execute("SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?", (uid, target)).fetchone()
        if ex:
            c.execute("DELETE FROM follows WHERE follower_id=? AND followee_id=?", (uid, target))
            return {"following": False}
        c.execute("INSERT OR IGNORE INTO follows (follower_id, followee_id) VALUES (?,?)", (uid, target))
        return {"following": True}


def wall(viewer_id: int, limit=60) -> list:
    return feed(viewer_id, ptype="transformation", limit=limit)


def stats() -> dict:
    _ensure()
    with db.cursor() as c:
        members = c.execute("SELECT COUNT(*) n FROM member_profiles").fetchone()["n"]
        posts = c.execute("SELECT COUNT(*) n FROM posts").fetchone()["n"]
    return {"members_display": _SOCIAL_BASE + members, "members": members, "posts": posts}
