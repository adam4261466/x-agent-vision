"""Simple X.com prospect and conversation assistant.

This is the platform-independent brain of the agent. It owns the CRM (users,
posts, events, DMs, drafts, settings, sweep sessions), the relationship state
machine, and the Ollama message generator.

The X platform adapter (x_browser.py) is a separate layer, exactly like the
LinkedIn browser adapter is for the LinkedIn agent. The brain never talks to
x.com directly - it tells the adapter what it needs (read a profile, read a DM
thread, type into the composer) and persists everything to SQLite, so an
interrupted sweep or an X outage never loses the workflow.
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_agent.db")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_agent_debug.log")

# DM/event types that give us EXPLICIT user intent - the only reason a first
# message may be drafted (X rules prohibit unsolicited bulk DMs).
INTENT_SIGNALS = {
    "OPTED_IN", "MENTION_RECEIVED", "DM_RECEIVED", "REQUEST_INFO",
    "FOLLOW_BACK", "POST_REPLY_INBOUND", "QUOTE_INBOUND",
}

# Default cap on cold (unsolicited) first-message drafts generated per sweep
# pass (15 of a typical 20-user pass). The human still edits and presses the
# final Send for each one; the cap just stops a sweep pass from ever turning
# into a blast. Can be raised in the GUI (Settings -> cold drafts per pass) and
# is persisted to the settings table.
MAX_COLD_DRAFTS_PER_PASS = 15


def _dbg(msg: str):
    try:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        thread = threading.current_thread().name
        line = f"[{ts}] [x_agent] [{thread}] {msg}"
        print(line, file=sys.stderr, flush=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            user_id TEXT UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            bio TEXT,
            location TEXT,
            website TEXT,
            followers_count INTEGER NOT NULL DEFAULT 0,
            following_count INTEGER NOT NULL DEFAULT 0,
            post_count INTEGER NOT NULL DEFAULT 0,
            verified INTEGER NOT NULL DEFAULT 0,
            profile_created_at TEXT,
            profile_fetched_at TEXT,
            status TEXT NOT NULL DEFAULT 'discovered',
            icp_score INTEGER NOT NULL DEFAULT 0,
            problem_signal INTEGER NOT NULL DEFAULT 0,
            eliminated INTEGER NOT NULL DEFAULT 0,
            elimination_reason TEXT,
            eliminated_at TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            post_id TEXT,
            text TEXT,
            like_count INTEGER NOT NULL DEFAULT 0,
            retweet_count INTEGER NOT NULL DEFAULT 0,
            reply_count INTEGER NOT NULL DEFAULT 0,
            quote_count INTEGER NOT NULL DEFAULT 0,
            url TEXT,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            content TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('outbound','inbound')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('initial','reply','cold')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS sweep_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sweep_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            query TEXT,
            icp TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_users_name ON users(display_name);
        CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id,created_at);
        CREATE INDEX IF NOT EXISTS idx_sweep_state ON sweep_state(sweep_id,user_id);
        """)
        _ensure_drafts_kind(db)
        _ensure_follow_columns(db)
        _dbg("init_db done")


def _ensure_drafts_kind(db) -> None:
    """SQLite cannot alter a CHECK constraint in place, so if a legacy drafts
    table only allows ('initial','reply') we rebuild it to also allow 'cold'."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='drafts'").fetchone()
    if row and row["sql"] and "'cold'" not in row["sql"]:
        _dbg("migrating drafts.kind CHECK to allow 'cold'")
        db.execute("""CREATE TABLE drafts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('initial','reply','cold')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        db.execute("""INSERT INTO drafts_new (id, user_id, kind, content, created_at)
            SELECT id, user_id, kind, content, created_at FROM drafts""")
        db.execute("DROP TABLE drafts")
        db.execute("ALTER TABLE drafts_new RENAME TO drafts")


def _ensure_follow_columns(db) -> None:
    """Add followed / followed_at / follows_you columns if they don't exist yet."""
    cols = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "followed" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN followed INTEGER NOT NULL DEFAULT 0")
        _dbg("migrate: added users.followed")
    if "followed_at" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN followed_at TEXT")
        _dbg("migrate: added users.followed_at")
    if "follows_you" not in cols:
        db.execute("ALTER TABLE users ADD COLUMN follows_you INTEGER NOT NULL DEFAULT 0")
        _dbg("migrate: added users.follows_you")


def reset_db() -> None:
    with connect() as db:
        db.execute("DROP TABLE IF EXISTS drafts")
        db.execute("DROP TABLE IF EXISTS messages")
        db.execute("DROP TABLE IF EXISTS events")
        db.execute("DROP TABLE IF EXISTS posts")
        db.execute("DROP TABLE IF EXISTS sweep_state")
        db.execute("DROP TABLE IF EXISTS campaigns")
        db.execute("DROP TABLE IF EXISTS users")
    init_db()


# ---------------------------------------------------------------------------
# Users (prospects on X)
# ---------------------------------------------------------------------------

def add_user(username: str, source: str = "manual") -> int:
    """Insert (or re-activate) a user by handle if they are not already tracked."""
    username = username.strip().lstrip("@").strip().lower()
    if not username:
        raise ValueError("Username cannot be empty")
    now = utc_now()
    with connect() as db:
        found = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if found:
            db.execute("UPDATE users SET eliminated=0, updated_at=? WHERE id=?", (now, found["id"]))
            return int(found["id"])
        cur = db.execute("""INSERT INTO users
            (username,display_name,status,source,icp_score,problem_signal,created_at,updated_at)
            VALUES (?,?,?,?,0,0,?,?)""", (username, username, "discovered", source, now, now))
        return int(cur.lastrowid)


def upsert_user(data: dict[str, Any]) -> int:
    """Merge a normalized user dict from the browser adapter into the CRM.

    'username' is the unique key. Missing rows are inserted as discovered and
    prefilled from the API; existing rows get their public data refreshed.
    """
    username = str(data.get("username") or "").strip().lstrip("@").strip().lower()
    if not username:
        raise ValueError("User dict must include a username")
    now = utc_now()
    display = data.get("display_name") or username
    with connect() as db:
        found = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if found:
            db.execute("""UPDATE users SET
                    user_id=?, display_name=?, bio=?, location=?, website=?,
                    followers_count=?, following_count=?, post_count=?, verified=?,
                    follows_you=?, profile_created_at=?, profile_fetched_at=?, updated_at=?
                WHERE id=?""",
                (data.get("user_id"), display, data.get("bio"), data.get("location"),
                 data.get("website"), int(data.get("followers_count") or 0),
                 int(data.get("following_count") or 0), int(data.get("post_count") or 0),
                 int(bool(data.get("verified"))), int(bool(data.get("follows_you"))),
                 data.get("profile_created_at"), now, now,
                 found["id"]))
            return int(found["id"])
        cur = db.execute("""INSERT INTO users
            (username,user_id,display_name,bio,location,website,followers_count,
             following_count,post_count,verified,follows_you,
             profile_created_at,profile_fetched_at,
             status,icp_score,problem_signal,source,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (username, data.get("user_id"), display, data.get("bio"), data.get("location"),
             data.get("website"), int(data.get("followers_count") or 0),
             int(data.get("following_count") or 0), int(data.get("post_count") or 0),
             int(bool(data.get("verified"))), int(bool(data.get("follows_you"))),
             data.get("profile_created_at"), now,
             "discovered", 0, 0, "discovery", now, now))
        return int(cur.lastrowid)


def search_users(query: str = "", limit: int = 1000) -> list[sqlite3.Row]:
    q = f"%{query.strip()}%"
    with connect() as db:
        return db.execute("""SELECT * FROM users
            WHERE (? = '%%' OR username LIKE ? COLLATE NOCASE
                   OR display_name LIKE ? COLLATE NOCASE OR bio LIKE ? COLLATE NOCASE)
            ORDER BY COALESCE(followers_count,0) DESC, username LIMIT ?""", (q,q,q,q,limit)).fetchall()


def get_user(user_id: int):
    with connect() as db:
        return db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_handle(username: str):
    username = username.strip().lstrip("@").lower()
    with connect() as db:
        return db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def set_user_status(user_id: int, status: str) -> None:
    with connect() as db:
        db.execute("UPDATE users SET status=?, updated_at=? WHERE id=?", (status, utc_now(), user_id))


def set_user_scores(user_id: int, icp: int = 0, problem: int = 0) -> None:
    with connect() as db:
        db.execute("UPDATE users SET icp_score=?, problem_signal=?, updated_at=? WHERE id=?",
                   (int(icp), int(problem), utc_now(), user_id))


# ---------------------------------------------------------------------------
# Sweep session management (identical pattern to the LinkedIn agent)
# ---------------------------------------------------------------------------

def start_sweep_session(max_per_session: int = 20) -> str:
    sweep_id = f"sweep_{int(datetime.now().timestamp() * 1000)}"
    now = utc_now()
    with connect() as db:
        db.execute("""
            INSERT INTO sweep_state (sweep_id, user_id, status, created_at)
            SELECT ?, id, 'pending', ?
            FROM users
            WHERE eliminated = 0
        """, (sweep_id, now))
        _dbg(f"start_sweep_session sweep_id={sweep_id} max={max_per_session}")
    return sweep_id


def get_sweep_queue(sweep_id: str, limit: int = 20) -> list:
    """Next `limit` users for this sweep session, advancing through the whole
    CRM instead of re-reading the same group every pass:
      1) users never successfully read before (discovery)
      2) then those read longest ago (LRU re-validation rotation)
    '#read' comes from any previous sweep session, so progress persists and a
    blocked user floats back up next pass."""
    with connect() as db:
        rows = db.execute("""
            SELECT u.*
            FROM users u
            JOIN sweep_state s ON s.user_id = u.id AND s.sweep_id = ?
            WHERE s.status = 'pending'
            ORDER BY
                (SELECT COUNT(*) FROM sweep_state s2
                   WHERE s2.user_id = u.id AND s2.status = 'read') ASC,
                (SELECT MAX(s2.created_at) FROM sweep_state s2
                   WHERE s2.user_id = u.id AND s2.status = 'read') ASC,
                u.id
            LIMIT ?
        """, (sweep_id, limit)).fetchall()
        return rows


def mark_swept(user_id: int, sweep_id: str, status: str) -> None:
    with connect() as db:
        db.execute("""
            UPDATE sweep_state SET status=?, created_at=? WHERE sweep_id=? AND user_id=?
        """, (status, utc_now(), sweep_id, user_id))
        _dbg(f"mark_swept uid={user_id} sweep={sweep_id} status={status}")


def sweep_session_summary(sweep_id: str) -> dict[str, int]:
    with connect() as db:
        rows = db.execute("""
            SELECT status, COUNT(*) AS cnt FROM sweep_state
            WHERE sweep_id=? GROUP BY status
        """, (sweep_id,)).fetchall()
        return {r["status"]: r["cnt"] for r in rows}


def is_sweep_complete(sweep_id: str) -> bool:
    with connect() as db:
        row = db.execute("""
            SELECT COUNT(*) AS cnt FROM sweep_state
            WHERE sweep_id=? AND status='pending'
        """, (sweep_id,)).fetchone()
        return row["cnt"] == 0


# ---------------------------------------------------------------------------
# Messages (DMs), events, posts
# ---------------------------------------------------------------------------

def get_messages(user_id: int):
    with connect() as db:
        return db.execute("SELECT * FROM messages WHERE user_id=? ORDER BY created_at,id", (user_id,)).fetchall()


def _text_exists_any_direction(user_id: int, content: str) -> bool:
    with connect() as db:
        return db.execute("SELECT 1 FROM messages WHERE user_id=? AND content=? LIMIT 1",
                          (user_id, content.strip())).fetchone() is not None


def add_message(user_id: int, direction: str, content: str, dedupe: bool = False) -> None:
    content = content.strip()
    if not content or direction not in {"outbound", "inbound"}:
        raise ValueError("Message must not be empty and direction must be outbound/inbound")
    if dedupe and _text_exists_any_direction(user_id, content):
        return
    with connect() as db:
        db.execute("INSERT INTO messages(user_id,direction,content,created_at) VALUES(?,?,?,?)",
                   (user_id, direction, content, utc_now()))


def record_sent(user_id: int, content: str) -> None:
    add_message(user_id, "outbound", content)


def save_draft(user_id: int, kind: str, content: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO drafts(user_id,kind,content,created_at) VALUES(?,?,?,?)",
                   (user_id, kind, content.strip(), utc_now()))


def add_event(user_id: int, event_type: str, content: str = "") -> None:
    with connect() as db:
        db.execute("INSERT INTO events(user_id,event_type,content,created_at) VALUES(?,?,?,?)",
                   (user_id, event_type, content, utc_now()))


# ---------------------------------------------------------------------------
# Follow management (X DM requires mutual follow for unverified accounts)
# ---------------------------------------------------------------------------

def mark_followed(username: str) -> None:
    """Record that we followed this user (call after follow_user succeeds)."""
    username = username.strip().lstrip("@").lower()
    with connect() as db:
        db.execute("UPDATE users SET followed=1, followed_at=?, updated_at=? "
                   "WHERE username=? AND followed=0",
                   (utc_now(), utc_now(), username))
        _dbg(f"mark_followed @{username}")


def update_follows_you(username: str, follows_you: bool) -> None:
    """Update the 'follows_you' field from a live profile scan."""
    username = username.strip().lstrip("@").lower()
    with connect() as db:
        db.execute("UPDATE users SET follows_you=?, updated_at=? WHERE username=?",
                   (int(follows_you), utc_now(), username))


def is_follow_eligible(followers: int, following: int, balance: float = 0.20,
                       max_followers: int = 400) -> bool:
    """A prospect is a follow target when EITHER:
      - they have under `max_followers` (default 400) followers (small account), OR
      - their followers and following counts are within `balance` (default 20%)
        of each other in both directions (a balanced peer).
    Any account that is small OR balanced is worth following."""

    def _balanced() -> bool:
        f = int(followers or 0)
        g = int(following or 0)
        if f <= 0 or g <= 0:
            return False
        return abs(f - g) * 1.0 / max(f, g) <= balance

    return int(followers or 0) <= max_followers or _balanced()


def get_follow_targets(limit: int = 50, balance: float = 0.20,
                       max_followers: int = 400) -> list[sqlite3.Row]:
    """Users eligible for a follow: not eliminated, not yet followed, and who
    are EITHER small (followers <= `max_followers`, default 400) OR balanced
    (followers and following within `balance`, default 20%). Small accounts are
    easy to reach; balanced peers are engaging - both respond to a follow."""
    with connect() as db:
        return db.execute("""
            SELECT *
            FROM users
            WHERE eliminated=0 AND followed=0
              AND (followers_count <= ?
                   OR (followers_count > 0 AND following_count > 0
                       AND (ABS(followers_count - following_count)
                            * 1.0 / MAX(followers_count, following_count)) <= ?))
            ORDER BY created_at DESC LIMIT ?
        """, (max_followers, balance, limit)).fetchall()


def get_pending_followbacks(limit: int = 50) -> list[sqlite3.Row]:
    """Users we've followed but who haven't followed back yet."""
    with connect() as db:
        return db.execute("""
            SELECT * FROM users
            WHERE followed=1 AND follows_you=0 AND eliminated=0
            ORDER BY followed_at DESC LIMIT ?
        """, (limit,)).fetchall()


def get_events(user_id: int) -> list[sqlite3.Row]:
    with connect() as db:
        return db.execute("SELECT * FROM events WHERE user_id=? ORDER BY created_at,id", (user_id,)).fetchall()


def store_posts(user_id: int, posts: list[dict[str, Any]]) -> int:
    """Insert (or refresh) a user's recent posts. Returns how many were new."""
    now = utc_now()
    added = 0
    with connect() as db:
        for p in posts:
            text = str(p.get("text") or "").replace("\u200c", "").strip()
            if not text:
                continue
            pid = str(p.get("post_id") or "").strip()
            exists = False
            if pid:
                exists = db.execute("SELECT 1 FROM posts WHERE user_id=? AND post_id=? LIMIT 1",
                                    (user_id, pid)).fetchone() is not None
            if not exists:
                db.execute("""INSERT INTO posts
                    (user_id,post_id,text,like_count,retweet_count,reply_count,quote_count,url,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (user_id, pid, text, int(p.get("like_count") or 0), int(p.get("retweet_count") or 0),
                     int(p.get("reply_count") or 0), int(p.get("quote_count") or 0), p.get("url"), now))
                added += 1
    return added


def get_recent_posts(user_id: int, max_posts: int = 20) -> list[sqlite3.Row]:
    with connect() as db:
        return db.execute("""SELECT * FROM posts WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?""",
                          (user_id, max_posts)).fetchall()


def format_recent_posts(user_id: int, max_posts: int = 10) -> str:
    posts = get_recent_posts(user_id, max_posts=max_posts)
    if not posts:
        return "No recent posts captured yet."
    lines = []
    for p in reversed(posts):  # oldest -> newest for reading
        metrics = []
        if p["like_count"]:
            metrics.append(f"{p['like_count']} likes")
        if p["retweet_count"]:
            metrics.append(f"{p['retweet_count']} rts")
        if p["reply_count"]:
            metrics.append(f"{p['reply_count']} replies")
        suffix = f"  ({', '.join(metrics)})" if metrics else ""
        lines.append(f"- {p['text']}{suffix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State machine (Python decides the state, never the LLM)
# ---------------------------------------------------------------------------

def _has_user_intent(user_id: int) -> bool:
    """True when there is an EXPLICIT inbound signal (opt-in, mention, request,
    etc.) that makes a first DM legal under X's rules."""
    with connect() as db:
        q = ",".join("?" for _ in INTENT_SIGNALS)
        row = db.execute(f"SELECT 1 FROM events WHERE user_id=? AND event_type IN ({q}) LIMIT 1",
                         (user_id, *INTENT_SIGNALS)).fetchone()
        if row:
            return True
        row2 = db.execute("SELECT 1 FROM messages WHERE user_id=? AND direction='inbound' LIMIT 1",
                          (user_id,)).fetchone()
        return row2 is not None


def has_user_intent(user_id: int) -> bool:
    """Public wrapper - exposes whether a first DM may cite an explicit signal."""
    return _has_user_intent(user_id)


def classify_prospect(row) -> tuple[str, str]:
    """Return (stage, action_text) for one user row based on recorded DMs/events.

    Stages:
      eliminated  - do not contact
      goal        - app link already sent, nothing new from them
      goal_reply  - app link sent AND they wrote back      -> generate a reply
      awaiting    - our last DM is pending their reply     -> wait
      reply       - their last DM needs an answer          -> generate a reply
      initial     - no DMs yet, but explicit intent signal -> generate first DM
      qualified   - no intent yet, signals analyzed        -> watch (no send)
      discovered  - just added, nothing analyzed           -> watch (no send)
    """
    if row["eliminated"]:
        return "eliminated", "Do not contact"
    msgs = get_messages(int(row["id"]))
    link = app_url()
    goal = any(m["direction"] == "outbound" and link and link.lower() in (m["content"] or "").lower()
               for m in msgs) if msgs else False
    if msgs:
        last = msgs[-1]
        if goal:
            if last["direction"] == "inbound":
                return "goal_reply", "Goal reached - they wrote back, send a reply"
            return "goal", "Done - app link sent"
        if last["direction"] == "outbound":
            return "awaiting", "Waiting for their reply"
        return "reply", "They replied - send your reply"
    if _has_user_intent(int(row["id"])):
        return "initial", "Explicit intent - generate a first DM for review"
    has_posts = bool(get_recent_posts(int(row["id"]), max_posts=1))
    if has_posts or row["icp_score"] > 0 or row["profile_fetched_at"]:
        return "qualified", "Signals analyzed - watch & engage, no DM yet"
    return "discovered", "Newly added - analyze their signals first"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str) -> None:
    with connect() as db:
        db.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value.strip()))


def app_url() -> str:
    url = os.environ.get("APP_URL", "").strip()
    if not url:
        url = get_setting("app_url").strip()
    return url


def product_name() -> str:
    return get_setting("product_name") or "Doxium"


def max_cold_drafts() -> int:
    """Cold first-message drafts allowed per sweep pass (Settings override)."""
    try:
        return max(1, int(get_setting("max_cold_drafts") or MAX_COLD_DRAFTS_PER_PASS))
    except (TypeError, ValueError):
        return MAX_COLD_DRAFTS_PER_PASS


def open_profile(username: str) -> None:
    if os.name == "nt":
        subprocess.Popen(["cmd", "/c", "start", "", f"https://x.com/{username.lstrip('@')}"], shell=False)
    else:
        import webbrowser
        webbrowser.open(f"https://x.com/{username.lstrip('@')}")


# ---------------------------------------------------------------------------
# Ollama integration
# ---------------------------------------------------------------------------

def _ollama_chat(messages: list[dict[str, str]], temperature: float = 0.65) -> str:
    _dbg(f"_ollama_chat START model={OLLAMA_MODEL} n_messages={len(messages)}")
    payload = {"model": OLLAMA_MODEL, "messages": messages, "stream": False,
               "options": {"temperature": temperature, "num_ctx": 32000}}
    req = urllib.request.Request(
        f"{OLLAMA_URL.rstrip('/')}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            data = json.loads(response.read().decode())
    except Exception as exc:
        _dbg(f"_ollama_chat FAILED: {type(exc).__name__}: {exc}")
        raise RuntimeError(f"Could not reach Ollama at {OLLAMA_URL}: {exc}") from exc
    text = data.get("message", {}).get("content", "").strip()
    if not text:
        raise RuntimeError("Ollama returned an empty message")
    return text


def _user_context(p) -> str:
    fmt = (
        f"Handle: @{p['username']}\n"
        f"Name: {p['display_name'] or p['username']}\n"
        f"Bio: {p['bio'] or 'No bio captured yet.'}\n"
        f"Location: {p['location'] or 'Unknown'}\n"
        f"Website: {p['website'] or 'None'}\n"
        f"Followers: {p['followers_count'] or 0}  Following: {p['following_count'] or 0}  "
        f"Posts: {p['post_count'] or 0}"
    )
    return fmt


def format_history(user_id: int, max_messages: int = 30) -> str:
    rows = get_messages(user_id)[-max_messages:]
    return "\n".join(f"{r['direction'].upper()}: {r['content']}" for r in rows) if rows else "No DM conversation yet."


def generate_initial(user_id: int) -> str:
    return _generate_first_dm(user_id, cold=False)


def generate_cold_initial(user_id: int) -> str:
    """Draft a first DM for a prospect with NO intent signal (cold outreach).

    Unsolicited, so the prompt must never pretend they showed interest, and it
    is saved under its own draft kind ('cold') to keep it distinct from
    intent-based first messages."""
    return _generate_first_dm(user_id, cold=True)


def _generate_first_dm(user_id: int, cold: bool) -> str:
    _dbg(f"generate_{'cold_initial' if cold else 'initial'} START user_id={user_id}")
    p = get_user(user_id)
    if not p:
        raise ValueError("User not found")
    if not app_url():
        _dbg("_generate_first_dm: no app link set, drafting intro without link")
    facts = format_recent_posts(user_id, max_posts=6)
    if cold:
        permission = ("This is COLD outreach: they have NOT signaled any interest. "
                      "Only open with a concrete reference to ONE of their RECENT "
                      "POSTS (the actual subject they post about - never generic "
                      "praise, never 'I saw your post', never pretend they know "
                      "you). Keep it under 2 short sentences, be genuinely "
                      "specific, and end with one light question.")
    else:
        permission = ("They gave some signal of interest (mention, request, "
                      "follow-back, or they followed you) - a first DM is OK here, "
                      "just don't sound salesy.")
    prompt = (
        "Write a short, NATURAL DM to this person on X. It must read like a real "
        "person who saw their post a minute ago and quickly typed something - "
        "NOT marketing, NOT a sales pitch, NOT a bot.\n"
        "RULES - follow exactly:\n"
        "- 1-2 short sentences max. Use contractions (you're, that's, I'd).\n"
        "- Sentence 1: name ONE concrete subject from their RECENT POSTS below "
        "(the actual case, app, tool, city, project, event they talked about). "
        "Never generic praise and never 'caught my eye' / 'I noticed' / 'I saw "
        "your post'.\n"
        "- Sentence 2: tied to that subject, ask one real, specific question "
        "about their work or perspective. No calls, no pricing, no link.\n"
        "- NEVER use: 'caught my eye', 'I'm building a tool', 'wanted to reach "
        "out', 'resonated', 'just wanted to', exclamation marks, jargon, or "
        "anything that sounds like a template.\n"
        "- If you mention what someone does, make it one conversational clause - "
        "e.g. 'we work on [domain] too' - never 'I'm building a tool for X'.\n"
        f"- {permission}\n"
        f"Your product/domain (mention naturally if it fits, else skip): {product_name()}\n\n"
        f"PERSON\n{_user_context(p)}\n\nRECENT POSTS\n{facts}\n\nCONVERSATION\n{format_history(user_id)}"
    )
    _dbg(f"generate_{'cold_initial' if cold else 'initial'} prompt len={len(prompt)}")
    draft = _ollama_chat(
        [{"role": "system", "content": (
            "You write X DMs the way a real, friendly person actually texts: 1-2 "
            "short sentences, contractions, one specific reference to what the "
            "person really posted, and a genuine question. You never open with "
            "praise or 'saw your post', never use template phrases like 'caught "
            "my eye' or 'I'm building a tool', never drop links or pricing, and "
            "you never sound like marketing.")},
         {"role": "user", "content": prompt}],
        temperature=0.9,
    )
    save_draft(user_id, "cold" if cold else "initial", draft)
    return draft


def _signals_interest(messages) -> bool:
    try:
        tail = (messages[-1]["content"] or "").lower()
    except Exception:
        return False
    hits = ["how do i", "how does", "what is", "what's", "link", "try it", "try the",
            "sign up", "use it", "demo", "interested", "send", "can i", "would like",
            "download", "where", "start", "pdf", "http", "let me see", "dm me the link",
            "send the link", "waitlist", "beta"]
    return any(h in tail for h in hits)


def generate_reply(user_id: int) -> str:
    _dbg(f"generate_reply START user_id={user_id}")
    p = get_user(user_id)
    if not p:
        raise ValueError("User not found")
    msgs = get_messages(user_id)
    interest = bool(msgs) and msgs[-1]["direction"] == "inbound" and _signals_interest(msgs)
    link = app_url()
    prompt = (
        "Write an X DM reply to the conversation below. RULES - follow exactly:\n"
        "- MAX 2 short sentences, casual like a friend texting back. Answer the LAST thing they asked.\n"
        "- NO compliments, NO analysis, NO professional language, NO exclamation marks.\n"
        "- If they asked how to use the product, what it is, for a link, or anything close to 'I want to try it' -> "
        "answer that directly and put the link on its own line, exactly as-is.\n"
        "- Otherwise (chit-chat, no interest yet) answer warmly WITHOUT a link and ask one light follow-up.\n"
    )
    if interest and link:
        prompt += (
            f"- THEY SIGNALED INTEREST in the last message. You MUST include this exact link "
            f"on its own line: {link}\n"
            f"- Example of the right feel: \"Easy - just drop a PDF in and ask it anything. "
            f"Here you go: {link} Want a quick walkthrough?\"\n")
    elif link:
        prompt += (f"- They have NOT asked for the product yet. Do NOT include a link now. "
                   f"(The link to use in a later reply is {link}.)\n")
    prompt += f"\nPERSON\n{_user_context(p)}\n\nCONVERSATION\n{format_history(user_id)}"
    draft = _ollama_chat(
        [{"role": "system", "content": (
            "You write extremely short, casual X DM replies - like a smart friend texting back. You always answer "
            "the exact question asked, and when the person clearly wants to try the product you give them the link "
            "right there. Two sentences max.")},
         {"role": "user", "content": prompt}],
        temperature=0.7,
    )
    save_draft(user_id, "reply", draft)
    return draft


def set_eliminated(user_id: int, eliminated: bool, reason: str | None = None) -> None:
    with connect() as db:
        db.execute("UPDATE users SET eliminated=?,elimination_reason=?,eliminated_at=?,updated_at=? WHERE id=?",
                   (int(eliminated), reason.strip() if eliminated and reason else None,
                    utc_now() if eliminated else None, utc_now(), user_id))


# ---------------------------------------------------------------------------
# Live import from the X platform adapter
# ---------------------------------------------------------------------------

def read_signals_live(username: str, user_id: int | None = None, session=None) -> dict:
    """Read the user's profile + recent posts from the live X page and persist
    them.

    Returns {"user": {..normalized..}, "new_posts": int}.
    """
    from x_browser import fetch_profile_page, fetch_user_posts_page
    _dbg(f"read_signals_live START username={username!r} user_id={user_id}")
    data = fetch_profile_page(username, session=session)
    uid = upsert_user(data)
    if "follows_you" in data:
        update_follows_you(username, bool(data.get("follows_you")))
    _, grams = _store_user_extra(uid, data)
    _dbg(f"read_signals_live user stored uid={uid}")
    time.sleep(random.uniform(1.5, 4.0))  # a person digests a profile before moving on
    posts = fetch_user_posts_page(username, count=25, session=session)
    new_posts = store_posts(uid, posts)
    _dbg(f"read_signals_live DONE uid={uid} new_posts={new_posts}")
    time.sleep(random.uniform(0.8, 2.5))  # skim notes, then proceed to the next step
    set_user_status(uid, "qualified")
    return {"user": data, "new_posts": new_posts, "uid": uid, "metrics": grams}


def _store_user_extra(uid: int, data: dict[str, Any]) -> tuple[int, dict]:
    """Store numeric metrics for scoring. Returns (user_id, metrics_dict)."""
    grams = {
        "followers": int(data.get("followers_count") or 0),
        "following": int(data.get("following_count") or 0),
        "posts": int(data.get("post_count") or 0),
    }
    return uid, grams


def read_dms_live(user_id: int, username: str, session=None) -> dict:
    """Read the DM thread with this user from the live X page and save messages.

    The browser layer attributes each message deterministically from the DOM
    (their messages carry the participant avatar, ours align right), so the
    brain trusts the adapter, same attribute-verification philosophy as the
    LinkedIn DOM reader.

    Returns {"messages": [...], "new": n}.
    """
    from x_browser import fetch_dm_thread
    _dbg(f"read_dms_live START user_id={user_id} username={username!r}")
    rows = fetch_dm_thread(username, display_name=None, session=session) or []
    new = 0
    for m in rows:
        direction = "inbound" if m.get("sender") == "them" else "outbound"
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        if _text_exists_any_direction(user_id, text):
            continue
        add_message(user_id, direction, text)
        new += 1
    _dbg(f"read_dms_live DONE messages={len(rows)} new={new}")
    return {"messages": rows, "new": new}


init_db()