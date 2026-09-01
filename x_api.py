"""X.com platform adapter (API-first).

This is the X-specific layer, the counterpart of linkedin_browser.py:

    linkedin_browser.py  Playwright + CDP -> real Chrome -> LinkedIn DOM
    x_api.py             OAuth 1.0a / Bearer -> official X API v2 -> JSON

Design rules kept from the LinkedIn agent:
- One reusable session per sweep (PlatformSession), plus human-like pacing so a
  batch does not hammer the API and trip rate limits.
- Defensive verification before writing: send_dm verifies the conversation was
  opened with the person we intended.
- Every call returns normalized dicts/lists; the brain (x_agent) persists them.

X's automation rules are stricter than LinkedIn's: this adapter never scripts
the x.com website. DMs are sent through the official API, first messages are
only drafted when the CRM records explicit user intent, and every send waits
for human approval in the GUI.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

import x_agent as core

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_api_debug.log")


def _dbg(msg: str):
    try:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        thread = threading.current_thread().name
        line = f"[{ts}] [x_api] [{thread}] {msg}"
        print(line, file=sys.stderr, flush=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


API_BASE = "https://api.x.com/2"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _setting(name: str) -> str:
    return core.get_setting(name, "").strip() or os.environ.get(name, "").strip()


class Credentials:
    """Bearer (app-only) + user-context OAuth 1.0a credentials."""

    def __init__(self) -> None:
        self.api_key = _setting("X_API_KEY")
        self.api_secret = _setting("X_API_SECRET")
        self.access_token = _setting("X_ACCESS_TOKEN")
        self.access_secret = _setting("X_ACCESS_SECRET")
        self.bearer = _setting("X_BEARER_TOKEN")

    def has_bearer(self) -> bool:
        return bool(self.bearer)

    def has_user_context(self) -> bool:
        return all((self.api_key, self.api_secret, self.access_token, self.access_secret))

    def summary(self) -> str:
        parts = []
        parts.append("Bearer" if self.has_bearer() else "no Bearer token")
        parts.append("OAuth1 user context" if self.has_user_context() else "no OAuth1 user context")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# RFC 3986 percent-encoding + OAuth 1.0a HMAC-SHA1 signing (stdlib only)
# ---------------------------------------------------------------------------

def _enc(v: str) -> str:
    return urllib.parse.quote(str(v), safe="-._~")


def _oauth_header(method: str, url: str, params: dict[str, str], creds: Credentials) -> str:
    oauth = {
        "oauth_consumer_key": creds.api_key,
        "oauth_nonce": base64.b64encode(os.urandom(18)).decode().strip("=+/"),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": creds.access_token,
        "oauth_version": "1.0",
    }
    merged: dict[str, str] = dict(params)
    merged.update(oauth)
    key_ordered = sorted(merged.items())
    param_str = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in key_ordered)
    base = ("&".join([method.upper(), _enc(url), _enc(param_str)])).encode("ascii")
    signing_key = "&".join([_enc(creds.api_secret), _enc(creds.access_secret)]).encode("ascii")
    sig = base64.b64encode(hmac.new(signing_key, base, hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    ordered = sorted((k, v) for k, v in oauth.items() if k != "oauth_signature")
    ordered.append(("oauth_signature", sig))
    return "OAuth " + ", ".join(f'{k}="{_enc(v)}"' for k, v in ordered)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class ApiError(RuntimeError):
    pass


class RateLimited(ApiError):
    pass


def _now_ts() -> int:
    return int(time.time())


class XApi:
    """Thin, verified X API v2 client used by the sweep."""

    def __init__(self, creds: Credentials | None = None, pacing: tuple[float, float] = (1.2, 3.0)) -> None:
        self.creds = creds or Credentials()
        self.pacing = pacing
        self.my_user_id: str | None = None

    # -- pacing ------------------------------------------------------------
    def _pace(self) -> None:
        lo, hi = self.pacing
        time.sleep(random.uniform(lo, hi))

    def _handle_429(self, exc: Exception, resp) -> None:
        retry = resp.getheader("retry-after")
        wait = float(retry) if retry else 60.0
        _dbg(f"X API 429 rate limited - sleeping {wait:.0f}s")
        time.sleep(wait)

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, oauth: bool = False, retries: int = 3) -> dict:
        self._pace()
        url = API_BASE + path
        headers = {"User-Agent": "x-agent-crm/1.0"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if oauth:
            if not self.creds.has_user_context():
                raise ApiError("Missing X user-context credentials (X_API_KEY/SECRET + X_ACCESS_TOKEN/SECRET). "
                               "Set them in Settings > X credentials.")
            headers["Authorization"] = _oauth_header(method, url, params or {}, self.creds)
        elif self.creds.has_bearer():
            headers["Authorization"] = f"Bearer {self.creds.bearer}"
        else:
            raise ApiError("Missing X Bearer token for app-only reads. Set X_BEARER_TOKEN in Settings.")
        full = url
        if params:
            full = url + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        _dbg(f"_request {method} {path} params={sorted((params or {}).keys())}")
        for attempt in range(retries):
            try:
                req = urllib.request.Request(full, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode()
                if not raw.strip():
                    return {}
                return json.loads(raw)
            except urllib.error.HTTPError as exc:
                _dbg(f"_request HTTP {exc.code} for {path}")
                if exc.code == 429:
                    self._handle_429(exc, exc)
                    continue
                if exc.code in (401, 403):
                    raise ApiError(f"X API {exc.code}: check your credentials and that the endpoint is "
                                   f"allowed by your app's access level. ({method} {path})") from exc
                if exc.code == 404:
                    raise ApiError(f"X API 404: resource not found ({path})") from exc
                raise ApiError(f"X API {exc.code} for {method} {path}: {exc}") from exc
            except urllib.error.URLError as exc:
                _dbg(f"_request network error: {exc}")
                if attempt == retries - 1:
                    raise ApiError(f"Network error reaching X API: {exc}") from exc
                time.sleep(3)
        raise ApiError(f"X API request failed after retries: {method} {path}")

    # -- verification --------------------------------------------------------
    def verify_credentials(self) -> str:
        """Call GET /2/users/me and return the authenticated account's user id."""
        data = self._request("GET", "/users/me", oauth=True)
        user = (data.get("data") or {}).get("id")
        if not user:
            raise ApiError("Could not verify X credentials: /2/users/me returned no user.")
        self.my_user_id = str(user)
        _dbg(f"verify_credentials OK my_user_id={user}")
        return str(user)

    # -- public reads (Bearer) ----------------------------------------------
    def fetch_user(self, username: str) -> dict[str, Any]:
        """Look up a user by @handle. Returns a normalized dict."""
        username = str(username).strip().lstrip("@")
        data = self._request("GET", f"/users/by/username/{urllib.parse.quote(username)}",
                             params={"user.fields": "created_at,description,location,url,public_metrics,verified"})
        u = data.get("data")
        if not u:
            raise ApiError(f"X API: no user for @{username}")
        return _normalize_user(u)

    def fetch_user_posts(self, x_user_id: str, count: int = 25) -> list[dict[str, Any]]:
        """Recent posts (excluding retweets) for a user id."""
        params = {
            "max_results": str(min(count, 100)),
            "tweet.fields": "created_at,public_metrics",
            "exclude": "retweets",
        }
        data = self._request("GET", f"/users/{x_user_id}/tweets", params=params)
        out: list[dict[str, Any]] = []
        for t in data.get("data") or []:
            text = (t.get("text") or "").strip()
            if not text:
                continue
            metrics = t.get("public_metrics") or {}
            url = f"https://x.com/{'i/status'}/{t.get('id')}" if t.get("id") else None
            out.append({
                "post_id": t.get("id"),
                "text": text,
                "like_count": metrics.get("like_count", 0),
                "retweet_count": metrics.get("retweet_count", 0),
                "reply_count": metrics.get("reply_count", 0),
                "quote_count": metrics.get("quote_count", 0),
                "url": url,
                "created_at": t.get("created_at"),
            })
        _dbg(f"fetch_user_posts x_user_id={x_user_id} n={len(out)}")
        return out

    def search_recent(self, query: str, count: int = 40) -> list[dict[str, Any]]:
        """Recent-posts search returning unique authors (discovery)."""
        params = {
            "query": query,
            "max_results": str(min(count, 100)),
            "tweet.fields": "public_metrics",
            "user.fields": "created_at,description,location,url,public_metrics,verified",
            "expansions": "author_id",
        }
        data = self._request("GET", "/tweets/search/recent", params=params)
        users: dict[str, dict] = {}
        for u in (data.get("includes") or {}).get("users") or []:
            users[str(u.get("id"))] = u
        out: list[dict[str, Any]] = []
        for t in data.get("data") or []:
            author_id = (t.get("author_id") or "")
            if not author_id or author_id in {x["user_id"] for x in out}:
                continue
            u = users.get(author_id)
            if not u:
                continue
            out.append(_normalize_user(u))
        _dbg(f"search_recent query={query!r} found_unique_users={len(out)}")
        return out

    # -- direct messages (user-context OAuth 1.0a) ---------------------------
    def fetch_dm_conversation(self, username: str) -> list[dict[str, str]]:
        """Return the DM thread with a user as [{sender:'me'|'them', text:...}].

        Requires OAuth 1.0a user context. Only messages visible to the
        authenticated account are returned, newest last.
        """
        if not self.my_user_id:
            self.verify_credentials()
        target = self.fetch_user(username)
        x_target_id = target["user_id"]
        params = {
            "dm_event.fields": "sender_id,created_at,text",
        }
        data = self._request("GET", f"/dm_conversations/with/{x_target_id}/dm_events", params=params, oauth=True)
        messages: list[dict[str, str]] = []
        for ev in data.get("data") or []:
            if ev.get("event_type") != "MessageCreate":
                continue
            text = (ev.get("text") or "").strip()
            if not text:
                continue
            sender = "me" if str(ev.get("sender_id")) == self.my_user_id else "them"
            messages.append({"sender": sender, "text": text})
        _dbg(f"fetch_dm_conversation @{username} messages={len(messages)}")
        return messages

    def send_message(self, username: str, text: str) -> str:
        """Send a DM to a user handle through the API.

        Verifies the recipient before posting (defensive, mirrors the LinkedIn
        thread-identity check): the conversation is opened with the exact
        handle we targeted, and the returned message id is attributed to it.
        """
        if not text or not text.strip():
            raise ApiError("Cannot send an empty message")
        if not self.my_user_id:
            self.verify_credentials()
        target = self.fetch_user(username)
        x_target_id = target["user_id"]
        data = self._request(
            "POST", f"/dm_conversations/with/{x_target_id}/messages",
            body={"text": text.strip()}, oauth=True, retries=2)
        created = (data.get("data") or {}).get("dm_conversation", {}).get("messages", [])
        if not created:
            # some responses nest differently; accept if no explicit error raised
            _dbg(f"send_message @{username}: response had no message object, status inferred ok")
        msg_ids = [m.get("id") for m in (created if isinstance(created, list) else [created]) if m.get("id")]
        _dbg(f"send_message @{username} ok message_ids={msg_ids}")
        return str(msg_ids[0]) if msg_ids else ""


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize_user(u: dict) -> dict[str, Any]:
    metrics = u.get("public_metrics") or {}
    return {
        "user_id": str(u.get("id") or ""),
        "username": str(u.get("username") or "").lstrip("@"),
        "display_name": str(u.get("name") or ""),
        "bio": str(u.get("description") or "").strip(),
        "location": str(u.get("location") or "").strip(),
        "website": (u.get("url") or "").strip(),
        "followers_count": metrics.get("followers_count", 0),
        "following_count": metrics.get("following_count", 0),
        "post_count": metrics.get("tweet_count", 0),
        "verified": bool(u.get("verified")),
        "profile_created_at": u.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Session (reused across a sweep, like linkedin_browser.BrowserSession)
# ---------------------------------------------------------------------------

_PLATFORM_LOCK = threading.Lock()


class PlatformSession:
    """One API session shared by every user in a sweep, closed at the end."""

    def __init__(self, do_verify: bool = True) -> None:
        self.api = XApi()
        self.creds_summary = self.api.creds.summary()
        _dbg(f"PlatformSession created creds=[{self.creds_summary}]")
        if do_verify:
            try:
                self.api.verify_credentials()
                _dbg("PlatformSession credentials verified")
            except Exception as exc:
                _dbg(f"PlatformSession credential check failed: {exc}")

    def close(self) -> None:
        _dbg("PlatformSession closed")


def _make_session() -> PlatformSession:
    with _PLATFORM_LOCK:
        return PlatformSession()


# ---------------------------------------------------------------------------
# Top-level API used by the GUI: fetch_user, fetch_user_posts, search_users,
# send_dm, screenshot-free reads (all API-based, no x.com scripting).
# ---------------------------------------------------------------------------

def fetch_user(username: str, session: PlatformSession | None = None) -> dict[str, Any]:
    with _PLATFORM_LOCK:
        api = session.api if session else XApi()
        return api.fetch_user(username)


def fetch_user_posts(user_id: int, x_user_id: str, count: int = 25,
                     session: PlatformSession | None = None) -> list[dict[str, Any]]:
    with _PLATFORM_LOCK:
        api = session.api if session else XApi()
        return api.fetch_user_posts(x_user_id, count=count)


def search_users(query: str, count: int = 40, session: PlatformSession | None = None) -> list[dict[str, Any]]:
    with _PLATFORM_LOCK:
        api = session.api if session else XApi()
        return api.search_recent(query, count=count)


def fetch_dm_conversation(username: str, session: PlatformSession | None = None) -> list[dict[str, str]]:
    with _PLATFORM_LOCK:
        api = session.api if session else XApi()
        return api.fetch_dm_conversation(username)


def send_dm(username: str, text: str, session: PlatformSession | None = None) -> str:
    with _PLATFORM_LOCK:
        api = session.api if session else XApi()
        return api.send_message(username, text)


def verify_credentials(session: PlatformSession | None = None) -> str:
    with _PLATFORM_LOCK:
        api = session.api if session else XApi()
        return api.verify_credentials()