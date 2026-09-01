"""Read X.com profile / posts / DMs via Chrome DevTools Protocol (CDP).

The browser-based counterpart of the LinkedIn agent's linkedin_browser.py.
Connects to an agent Chrome instance launched with --remote-debugging-port
(its own profile, so it never collides with your LinkedIn agent browser), reads
profiles and DM threads straight from the live X web DOM, and type drafts into
the DM composer. It never presses the final Send button: the human reviews the
typed message in X and clicks Send there.

Design rules mirrored from the LinkedIn agent:
- One reusable BrowserSession per sweep, human-paced reads, no bulk actions.
- Deterministic reads: senders are attributed from the DOM (their messages
  carry the participant avatar, ours align right), never guessed by the LLM.
- Verify before acting: a DM thread is only used after the opened conversation
  proves it belongs to @handle.

The X CDP endpoint defaults to http://127.0.0.1:9223 and can be overridden
with X_CDP_URL. Automating x.com is against X's rules: this layer only reads
public data the agent's own logged-in browser can already see, types drafts the
human approves into the composer, and never hits send.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "x_browser_debug.log")


def _dbg(msg: str):
    try:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        thread = threading.current_thread().name
        line = f"[{ts}] [x_browser] [{thread}] {msg}"
        print(line, file=sys.stderr, flush=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


CDP_URL = os.environ.get("X_CDP_URL", "http://127.0.0.1:9223")
CDP_PORT = int(CDP_URL.rsplit(":", 1)[-1].rstrip("/").split("/")[0])

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

AGENT_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".agent-x-chrome-profile")

# All CDP work shares one Chrome: serialize every browser operation so
# background sweep work never interleaves with manual actions.
_BROWSER_LOCK = threading.Lock()


def _chrome_exe() -> str:
    for p in CHROME_PATHS:
        if os.path.exists(p):
            return p
    return "chrome"


def _cdp_up() -> bool:
    try:
        r = requests.get(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2)
        _dbg(f"_cdp_up OK port={CDP_PORT} status={r.status_code}")
        return r.status_code == 200
    except Exception as e:
        _dbg(f"_cdp_up FAIL port={CDP_PORT}: {type(e).__name__}: {e}")
        return False


def _clear_profile_locks():
    for fname in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        fpath = os.path.join(AGENT_PROFILE_DIR, fname)
        try:
            if os.path.exists(fpath) or os.path.islink(fpath):
                os.remove(fpath)
        except Exception:
            pass


def _launch_chrome():
    exe = _chrome_exe()
    _dbg(f"_launch_chrome exe={exe!r} profile={AGENT_PROFILE_DIR!r}")
    if not os.path.exists(exe):
        _dbg(f"_launch_chrome FAILED: {exe} does not exist")
        raise RuntimeError(f"Chrome not found at any known path.")
    os.makedirs(AGENT_PROFILE_DIR, exist_ok=True)
    _clear_profile_locks()
    time.sleep(0.5)
    cmd = [
        exe,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={AGENT_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "about:blank",
    ]
    _dbg(f"_launch_chrome cmd={cmd}")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for i in range(40):
        time.sleep(0.5)
        if _cdp_up():
            _dbg(f"_launch_chrome OK: CDP up after ~{(i + 1) * 0.5:.1f}s")
            return True
    _dbg(f"_launch_chrome FAIL: CDP port {CDP_PORT} never came up after 20s")
    return False


def _ensure_chrome() -> str | None:
    """Return None if Chrome is reachable, else an error string."""
    if _cdp_up():
        return None
    _dbg("_ensure_chrome: CDP down, launching Chrome...")
    if _launch_chrome():
        return None
    err = (
        f"Could not start Chrome with remote debugging on port {CDP_PORT}.\n"
        f"Chrome path: {_chrome_exe()}\n"
        "Check that no antivirus/EDR is blocking --remote-debugging-port."
    )
    _dbg(f"_ensure_chrome FAILED: {err}")
    return err


# ---------------------------------------------------------------------------
# Human-like pacing: random pauses, jitter scrolling, variable typing. The X
# web app and its rate limits look at real human behavior, so no two actions
# take the same amount of time.
# ---------------------------------------------------------------------------

def _human_pause(lo_ms: float, hi_ms: float) -> None:
    """Sleep a random human-like pause between lo_ms and hi_ms."""
    time.sleep(random.uniform(lo_ms, hi_ms) / 1000.0)


def _human_wait(page: Any, lo_ms: float, hi_ms: float) -> None:
    """Page-level wait with a random duration (mimics a human reading pause)."""
    page.wait_for_timeout(random.uniform(lo_ms, hi_ms))


_SCROLL_JITTER_JS = """() => {
    const dir = Math.random() < 0.5 ? -1 : 1;
    const amt = 120 + Math.random() * 420;
    window.scrollBy(0, dir * amt);
    return true;
}"""


def _jitter_scroll(page: Any) -> None:
    """Small random scroll nudges (up/down a little) to look like a person
    skimming the page, with a short random pause after."""
    try:
        page.evaluate(_SCROLL_JITTER_JS)
        page.evaluate(_SCROLL_JITTER_JS)
    except Exception:
        pass
    _human_wait(page, 250, 800)


def _type_human(page: Any, text: str) -> None:
    """Type like a person: most keystrokes 25-120ms apart, an occasional
    'thinking' pause mid-message, and a longer pause before the final burst.
    Randomness means the same draft is never typed at the same speed twice."""
    n = len(text)
    for i, ch in enumerate(text):
        page.keyboard.type(ch, delay=0)
        if random.random() < 0.10:
            _human_pause(250, 700)          # human pause before a burst
        elif i == n - 1:
            _human_pause(180, 450)          # trailing pause before send
        else:
            _human_pause(22, 130)
        if (i + 1) % 4 == 0 and random.random() < 0.5:
            _jitter_scroll(page)


def _connect_sync():
    """Connect via CDP and return a (sync_playwright, browser, context) tuple."""
    err = _ensure_chrome()
    if err:
        raise RuntimeError(err)
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
    except Exception as exc:
        pw.stop()
        raise RuntimeError(
            f"Could not attach to Chrome on {CDP_URL}.\n"
            f"Make sure Chrome is running with --remote-debugging-port={CDP_PORT}."
        ) from exc
    if not browser.contexts:
        browser.close()
        pw.stop()
        raise RuntimeError("Chrome is connected, but no browser context is available.")
    return pw, browser, browser.contexts[0]


class BrowserSession:
    """A reusable CDP handle (playwright driver + browser + context). Create it
    once at the start of a sweep, pass it through every browser call, and close
    it when the sweep ends."""

    def __init__(self) -> None:
        _dbg("BrowserSession.acquire")
        pw, browser, context = _connect_sync()
        self.pw = pw
        self.browser = browser
        self.context = context
        _dbg("BrowserSession acquired")

    def close(self) -> None:
        _dbg("BrowserSession.close")
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared page helpers
# ---------------------------------------------------------------------------

_LOGGED_OUT_JS = """() => {
    if (document.querySelector('[data-testid="loginButton"], a[href="/login"], a[href="/signup"]')) return true;
    const t = (document.body ? document.body.innerText : '') || '';
    if (/sign in to x|log in to twitter|new to x\\?/i.test(t.slice(0, 2000))) return true;
    return false;
}"""


def _is_logged_out(page: Any) -> bool:
    try:
        return bool(page.evaluate(_LOGGED_OUT_JS))
    except Exception:
        return False


def _require_login(page: Any) -> None:
    if not _is_logged_out(page):
        return
    raise RuntimeError(
        "NOT LOGGED IN - the agent Chrome is signed out of X. Log in once at "
        "https://x.com/ in the agent Chrome (it uses its own profile on port "
        f"{CDP_PORT}) and rerun."
    )


def _hard_error(page: Any, hint: str) -> None:
    """Raise a clear error when the page shows an X error wall instead of data."""
    try:
        t = page.evaluate("() => (document.body ? document.body.innerText : '') || ''") or ""
    except Exception:
        t = ""
    if re.search(r"something went wrong|try reloading|this page doesn", t[:4000], re.I):
        raise RuntimeError(
            f"X returned an error page while reading {hint}. Reload the agent "
            "Chrome or wait a moment, then rerun."
        )


def _goto(page: Any, url: str, wait_selector: str | None = None, timeout_ms: int = 40000,
          wait_timeout_ms: int = 12000) -> None:
    _dbg(f"_goto {url!r}")
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if wait_selector:
        try:
            page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)
        except PlaywrightError:
            pass
    _human_wait(page, 500, 1500)   # random settle after every navigation
    _require_login(page)
    _hard_error(page, url)


def _close_stray_pages(context, before_ids: set[int]) -> None:
    try:
        for p in [p for p in context.pages if not p.is_closed()]:
            if id(p) not in before_ids:
                _dbg(f"closing stray tab url={(p.url or '')[:80]!r}")
                try:
                    p.close()
                except Exception:
                    pass
    except Exception:
        pass


def _parse_count(v: str) -> int:
    """'13.4K' -> 13400; '2M' -> 2000000; '1,234' -> 1234."""
    v = (v or "").replace(",", "").strip().upper()
    m = re.match(r"^([0-9.]+)([KMB]?)$", v)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    mult = {"": 1, "K": 1000, "M": 1000000, "B": 1000000000}[m.group(2)]
    return int(n * mult)


# ---------------------------------------------------------------------------
# Profile page (public data the logged-in browser can already see)
# ---------------------------------------------------------------------------

_PROFILE_NAME_JS = """() => {
    const _parseCount = (v) => {
        const s = (v || '').replace(/,/g, '').trim().toUpperCase();
        const m = s.match(/^([0-9.]+)([KMB]?)$/);
        if (!m) return 0;
        const n = parseFloat(m[1]); if (isNaN(n)) return 0;
        const mult = {'':1,'K':1000,'M':1000000,'B':1000000000}[m[2]];
        return Math.floor(n * mult);
    };
    const out = {name:'', handle:'', bio:'', location:'', website:'', followers:0, following:0, posts:0, verified:false};
    const n = document.querySelector('[data-testid="UserName"]');
    if (n) {
        const t = (n.innerText || '').replace(/\\s+/g, ' ').trim();
        const at = t.lastIndexOf('@');
        if (at > 0) {
            let nm = t.slice(0, at).replace(/\\s+$/,'');
            nm = nm.replace(/\\s+(Verified|Follows? you)\\s*$/i, '');
            out.name = nm.trim();
            const mh = t.slice(at).match(/@([A-Za-z0-9_]+)/);
            if (mh) out.handle = mh[1];
        } else {
            out.name = t;
        }
        out.verified = !!n.querySelector('[data-testid="icon-verified"], svg[aria-label*="Verified"], svg[data-testid="icon-badge"]');
    }
    const b = document.querySelector('[data-testid="UserDescription"]');
    if (b) out.bio = (b.innerText || '').trim();
    const items = document.querySelector('[data-testid="UserProfileHeader_Items"]');
    if (items) {
        const lines = (items.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
        for (const ln of lines) {
            if (!ln) continue;
            if (/^(https?:\\/\\/|www\\.)/i.test(ln) && ln.indexOf(' ') === -1) {
                if (!out.website) out.website = ln.replace(/^https?:\\/\\//i, '').replace(/\\/$/, '');
            } else if (/^[^\\s]+,\\s[^\\s]/.test(ln) && !out.location) {
                out.location = ln;
            }
        }
    }
    const anchors = Array.from(document.querySelectorAll('a[href][role="link"]'));
    for (const a of anchors) {
        const t = (a.innerText || '').replace(/\\s+/g, ' ').trim();
        const mt = t.match(/^([0-9.,KM]+)\\s+(Followers?|Following|Posts?|Reposts?|Likes?)$/);
        if (!mt) continue;
        const key = mt[2].toLowerCase();
        const val = _parseCount(mt[1]);
        if (key === 'followers') out.followers = val;
        else if (key === 'following') out.following = val;
        else if (key === 'posts') out.posts = val;
    }
    return out;
}"""


def _profile_fetch(page: Any, handle: str, timeout_ms: int) -> dict[str, Any]:
    url = f"https://x.com/{handle}"
    _goto(page, url, wait_selector='[data-testid="UserName"], [data-testid="UserDescription"], main',
          timeout_ms=timeout_ms)
    try:
        page.wait_for_selector('[data-testid="UserName"]', timeout=10000)
    except PlaywrightError:
        pass
    _human_wait(page, 900, 2400)          # 'read' the profile header like a person
    try:
        page.evaluate(_SCROLL_JITTER_JS)  # occasional skim nudge while looking
    except Exception:
        pass
    _human_wait(page, 500, 1400)
    out = page.evaluate(_PROFILE_NAME_JS) or {}
    # Fallback: name from the page title ("@handle (Display Name) / X").
    if not out.get("name"):
        title = page.title().strip()
        mt = re.search(r"^@([A-Za-z0-9_]+)\s*\((.+?)\)\s*/\s*X$", title)
        if mt:
            out["name"] = mt.group(2).strip()
            out["handle"] = out.get("handle") or mt.group(1)
    if not out.get("name") and not out.get("handle"):
        _hard_error(page, handle)
        raise RuntimeError(
            f"Could not read profile data for @{handle}. X returned neither a name "
            "nor a handle - the page structure may have changed or the account "
            "needs a moment to load in the agent Chrome."
        )
    _dbg(f"_profile_fetch @{handle} name={out.get('name')!r} bio_len={len(out.get('bio') or '')} "
         f"followers={out.get('followers')}")
    _human_wait(page, 700, 1700)          # linger before moving on to the next tab
    return {
        "username": (out.get("handle") or handle).lstrip("@").lower(),
        "display_name": out.get("name") or handle,
        "bio": out.get("bio") or "",
        "location": out.get("location") or "",
        "website": out.get("website") or "",
        "followers_count": out.get("followers") or 0,
        "following_count": out.get("following") or 0,
        "post_count": out.get("posts") or 0,
        "verified": bool(out.get("verified")),
    }


def fetch_profile_page(handle: str, timeout_ms: int = 40000,
                       session: BrowserSession | None = None) -> dict[str, Any]:
    """Read a profile (name, handle, bio, location, website, metrics) from the
    live X page. Reuses `session` when supplied (sweep keeps one connection),
    otherwise opens a fresh one and tears it down afterwards."""
    _dbg(f"fetch_profile_page START handle={handle!r} session={session is not None}")
    with _BROWSER_LOCK:
        pw = browser = context = None
        teardown = session is None
        if session is not None:
            context = session.context
        else:
            pw, browser, context = _connect_sync()
        before = {id(p) for p in context.pages}
        page = context.new_page()
        try:
            return _profile_fetch(page, str(handle).strip().lstrip("@"), timeout_ms)
        finally:
            try:
                page.close()
            except Exception:
                pass
            _close_stray_pages(context, before)
            if teardown:
                browser.close()
                pw.stop()


# ---------------------------------------------------------------------------
# Profile timeline (recent posts, excluding retweets/ads)
# ---------------------------------------------------------------------------

_SCROLL_TIMELINE_JS = """() => {
    const before = window.scrollY;
    window.scrollTo(0, document.body.scrollHeight);
    document.documentElement.scrollTop = document.documentElement.scrollHeight;
    return window.scrollY !== before;
}"""


_TWEET_EXTRACT_JS = """() => {
    const _parseCount = (v) => {
        const s = (v || '').replace(/,/g, '').trim().toUpperCase();
        const m = s.match(/^([0-9.]+)([KMB]?)$/);
        if (!m) return 0;
        const n = parseFloat(m[1]); if (isNaN(n)) return 0;
        const mult = {'':1,'K':1000,'M':1000000,'B':1000000000}[m[2]];
        return Math.floor(n * mult);
    };
    const rows = Array.from(document.querySelectorAll('[data-testid="tweet"]'));
    const out = [];
    for (const row of rows) {
        if (row.offsetParent === null) continue;
        const scEl = row.querySelector('[data-testid="socialContext"]');
        const sc = scEl ? (scEl.innerText || '') : '';
        if (/promoted|you might like|reposted|retweeted|you reposted/i.test(sc)) continue;
        const txtEl = row.querySelector('[data-testid="tweetText"]');
        const text = txtEl ? (txtEl.innerText || '').replace(/\\u200c/g, '').trim() : '';
        if (!text) continue;
        let post_id = '';
        const link = row.querySelector('a[href*="/status/"]');
        if (link) {
            const m = (link.getAttribute('href') || '').match(/\\/status\\/(\\d+)/);
            if (m) post_id = m[1];
        }
        const grab = (tid) => {
            const el = row.querySelector('[data-testid="' + tid + '"]');
            if (!el) return 0;
            const t = (el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '');
            const m = t.match(/([0-9.,KM]+)/);
            return m ? _parseCount(m[1]) : 0;
        };
        out.push({
            post_id: post_id,
            text: text,
            like_count: grab('like'),
            retweet_count: grab('retweet'),
            reply_count: grab('reply'),
            quote_count: 0
        });
    }
    return out;
}"""


def fetch_user_posts_page(handle: str, count: int = 25, scroll_rounds: int = 2,
                          timeout_ms: int = 40000,
                          session: BrowserSession | None = None) -> list[dict[str, Any]]:
    """Read recent posts from the profile timeline (scrolling gently), skipping
    retweets and ads."""
    _dbg(f"fetch_user_posts_page START handle={handle!r} count={count}")
    with _BROWSER_LOCK:
        pw = browser = context = None
        teardown = session is None
        if session is not None:
            context = session.context
        else:
            pw, browser, context = _connect_sync()
        before = {id(p) for p in context.pages}
        page = context.new_page()
        try:
            url = f"https://x.com/{str(handle).strip().lstrip('@')}"
            _goto(page, url, wait_selector='[data-testid="tweetText"], [data-testid="tweet"], main',
                  timeout_ms=timeout_ms)
            try:
                page.wait_for_selector('[data-testid="tweet"], [data-testid="tweetText"]', timeout=10000)
            except PlaywrightError:
                pass
            _human_wait(page, 1100, 2500)  # start reading the visible posts before scrolling
            collected: list[dict[str, Any]] = []
            seen: set[str] = set()
            for _r in range(scroll_rounds):
                try:
                    page.evaluate(_SCROLL_TIMELINE_JS)
                except Exception:
                    pass
                _human_wait(page, 1500, 3200)  # 'absorb' the newly scrolled posts
                _jitter_scroll(page)
                rows = page.evaluate(_TWEET_EXTRACT_JS) or []
                for r in rows:
                    key = r.get("post_id") or r.get("text")[:80]
                    if key in seen:
                        continue
                    seen.add(key)
                    collected.append(r)
                if len(collected) >= count:
                    break
            _dbg(f"fetch_user_posts_page @{handle} n={len(collected)}")
            _human_wait(page, 800, 1800)  # linger over the last posts before leaving
            return collected[:count]
        finally:
            try:
                page.close()
            except Exception:
                pass
            _close_stray_pages(context, before)
            if teardown:
                browser.close()
                pw.stop()


# ---------------------------------------------------------------------------
# Search (recent tweets -> unique authors, browser discovery)
# ---------------------------------------------------------------------------

_SEARCH_AUTHORS_JS = """() => {
    const seen = {};
    const rows = Array.from(document.querySelectorAll('[data-testid="tweet"]'));
    for (const row of rows) {
        if (row.offsetParent === null) continue;
        const scEl = row.querySelector('[data-testid="socialContext"]');
        const sc = scEl ? (scEl.innerText || '') : '';
        if (/promoted|you might like/i.test(sc)) continue;
        const link = row.querySelector('a[href^="/"][role="link"]');
        if (!link) continue;
        const href = link.getAttribute('href') || '';
        const m = href.match(/^\\/([A-Za-z0-9_]+)$/);
        if (!m) continue;
        const h = m[1].toLowerCase();
        if (/^(home|explore|settings|account|search|i|messages|notifications|compose)$/.test(h)) continue;
        if (seen[h]) continue;
        let display = m[1];
        const nameEl = row.querySelector('[data-testid="User-Name"]');
        if (nameEl) {
            const t = (nameEl.innerText || '').replace(/\\s+/g, ' ').trim();
            const without = t.replace(/\\s*@[A-Za-z0-9_]+\\s*$/, '').trim();
            if (without) display = without;
        }
        seen[h] = {username: m[1], display_name: display};
    }
    return Object.values(seen);
}"""


def search_recent_users(query: str, count: int = 40, scroll_rounds: int = 3,
                        timeout_ms: int = 40000,
                        session: BrowserSession | None = None) -> list[dict[str, Any]]:
    """Search recent tweets on x.com and return the unique author handles with
    their display names, at a human reading pace (variable scroll pauses, no
    bulk scanning). Profile details come later via fetch_profile_page."""
    _dbg(f"search_recent_users START query={query!r} count={count}")
    with _BROWSER_LOCK:
        pw = browser = context = None
        teardown = session is None
        if session is not None:
            context = session.context
        else:
            pw, browser, context = _connect_sync()
        before = {id(p) for p in context.pages}
        page = context.new_page()
        try:
            import urllib.parse as _up
            url = ("https://x.com/search?" + _up.urlencode(
                {"q": query, "src": "typed_query", "f": "live"}))
            _goto(page, url, wait_selector='[data-testid="tweet"], main', timeout_ms=timeout_ms)
            try:
                page.wait_for_selector('[data-testid="tweet"]', timeout=12000)
            except PlaywrightError:
                pass
            _human_wait(page, 1300, 2800)  # scan the first results before scrolling
            users: list[dict[str, Any]] = []
            seen: set[str] = set()
            for _r in range(scroll_rounds):
                try:
                    page.evaluate(_SCROLL_TIMELINE_JS)
                except Exception:
                    pass
                _human_wait(page, 1400, 3000)
                _jitter_scroll(page)
                for u in page.evaluate(_SEARCH_AUTHORS_JS) or []:
                    h = (u.get("username") or "").lower()
                    if h and h not in seen:
                        seen.add(h)
                        users.append(u)
                if len(users) >= count:
                    break
            _dbg(f"search_recent_users query={query!r} unique={len(users)}")
            _human_wait(page, 700, 1500)  # linger before leaving the search
            return users[:count]
        finally:
            try:
                page.close()
            except Exception:
                pass
            _close_stray_pages(context, before)
            if teardown:
                browser.close()
                pw.stop()


# ---------------------------------------------------------------------------
# DMs
# ---------------------------------------------------------------------------

_OPEN_THREAD_JS = """(handle) => {
    const entries = Array.from(document.querySelectorAll('[data-testid="conversation"]'));
    if (!entries.length) return false;
    const h = (handle || '').toLowerCase();
    const hit = entries.find(e => {
        const own = e.querySelector('a[href="/' + handle + '"]');
        if (own) return true;
        const t = (e.innerText || '').toLowerCase();
        return (h && t.includes('@' + h)) || (t.indexOf(h) === 0);
    });
    if (!hit) return false;
    const clickable = hit.querySelector('a[role="link"], a[href]') || hit;
    clickable.click();
    return true;
}"""

_VERIFY_THREAD_JS = """(handle) => {
    const entries = document.querySelectorAll('[data-testid="messageEntry"]').length;
    const headerLink = document.querySelector('a[href="/' + handle + '"]');
    return {entries: entries, header: !!headerLink};
}"""

_READ_MESSAGES_JS = """() => {
    const W = window.innerWidth;
    const entries = Array.from(document.querySelectorAll('[data-testid="messageEntry"]'));
    const out = [];
    for (const el of entries) {
        if (el.offsetParent === null) continue;
        const txtEl = el.querySelector('[data-testid="messageText"]');
        let text = txtEl ? (txtEl.innerText || '').trim() : '';
        if (!text) text = (el.innerText || '').trim();
        if (!text) continue;
        const r = el.getBoundingClientRect();
        const center = r.left + r.width / 2;
        const hasAvatar = !!el.querySelector('img[src*="profile_images"], [data-testid="messageAvatar"]');
        const them = hasAvatar || center < W * 0.5;
        out.push({sender: them ? 'them' : 'me', text: text});
    }
    return out;
}"""


def _open_thread(context, page: Any, handle: str, timeout_ms: int) -> Any:
    """Open the DM conversation with @handle from the inbox and verify it is
    really theirs. Returns the opened thread page or raises a clear error."""
    handle = str(handle).strip().lstrip("@").lower()
    _goto(page, "https://x.com/messages",
          wait_selector='[data-testid="conversation"], [data-testid="messageEntry"]', timeout_ms=timeout_ms)
    try:
        page.wait_for_selector('[data-testid="conversation"]', timeout=12000)
    except PlaywrightError:
        pass
    _human_wait(page, 1000, 2400)  # scan the conversation list before clicking one

    state_checked = False
    for _ in range(6):
        state_checked = True
        try:
            clicked = bool(page.evaluate(_OPEN_THREAD_JS, handle))
        except PlaywrightError:
            clicked = False
        if clicked:
            break
        _human_wait(page, 600, 1100)
    if not clicked and not page.evaluate("() => document.querySelectorAll('[data-testid=conversation]').length"):
        raise RuntimeError(
            "NO DM INBOX - the agent Chrome is on X but the Messages inbox did not "
            "load. Open https://x.com/messages in the agent Chrome once (you must "
            "have DMs available on your X account) and rerun."
        )

    # Verify the opened conversation belongs to @handle before reading it.
    verified = False
    thread = page
    for _ in range(16):
        thread = page
        try:
            ver = thread.evaluate(_VERIFY_THREAD_JS, handle) or {}
        except PlaywrightError:
            ver = {}
        if ver.get("entries") or ver.get("header"):
            if ver.get("header"):
                verified = True
                break
        _human_wait(thread, 500, 950)
    if not verified:
        raise RuntimeError(
            f"COULD NOT VERIFY - X did not confirm the open DM thread belongs to "
            f"@{handle}. Refusing to read or type into the wrong conversation."
        )
    _dbg(f"_open_thread @{handle} verified")
    return thread


def fetch_dm_thread(handle: str, display_name: str | None = None,
                    timeout_ms: int = 40000,
                    session: BrowserSession | None = None) -> list[dict[str, str]]:
    """Read the DM thread with @handle from the DOM.

    Returns [{"sender": "me"|"them", "text": ...}]. Senders are attributed
    deterministically (their messages carry the participant avatar, ours align
    right) - the browser layer never guesses, the app decides what a name
    means."""
    _dbg(f"fetch_dm_thread START handle={handle!r} name={display_name!r}")
    with _BROWSER_LOCK:
        pw = browser = context = None
        teardown = session is None
        if session is not None:
            context = session.context
        else:
            pw, browser, context = _connect_sync()
        before = {id(p) for p in context.pages}
        page = context.new_page()
        try:
            thread = _open_thread(context, page, str(handle).strip().lstrip("@"), timeout_ms)
            got: list[dict[str, str]] = []
            for _attempt in range(3):
                try:
                    raw = thread.evaluate(_READ_MESSAGES_JS) or []
                except PlaywrightError:
                    raw = []
                if raw or _attempt == 2:
                    got = [{"sender": m.get("sender", "them"), "text": str(m.get("text", "")).strip()}
                           for m in raw if m.get("text")]
                    break
                _human_wait(thread, 600, 1100)
            _dbg(f"fetch_dm_thread @{handle} messages={len(got)}")
            return got
        finally:
            try:
                page.close()
            except Exception:
                pass
            _close_stray_pages(context, before)
            if teardown:
                browser.close()
                pw.stop()


# ---------------------------------------------------------------------------
# Compose a draft into the DM composer (human presses the final Send)
# ---------------------------------------------------------------------------

_FOCUS_COMPOSER_JS = """() => {
    const sels = [
        '[data-testid="dmComposerTextInput"]',
        'div[data-testid="dmComposerTextInput"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
        'textarea'
    ];
    for (const s of sels) {
        const el = document.querySelector(s);
        if (el) { el.focus(); el.click(); return true; }
    }
    return false;
}"""

_COMPOSER_TEXT_JS = """() => {
    const sels = [
        '[data-testid="dmComposerTextInput"]',
        'div[contenteditable="true"][role="textbox"]',
        'div[contenteditable="true"]',
        'textarea'
    ];
    for (const s of sels) {
        const el = document.querySelector(s);
        if (el) {
            const t = ((el.innerText || '') || (el.value || '')).trim();
            if (t) return t;
        }
    }
    return '';
}"""


def send_message(handle: str, text: str, timeout_ms: int = 40000,
                 session: BrowserSession | None = None) -> str:
    """Open the DM thread with @handle and TYPE the approved draft into the
    composer. The agent never presses Send - the human reviews it in X and
    clicks the actual Send button there (X forbids automated posting, and a
    wrongly-auto-sent message is unrecoverable).

    Returns the composer's echoed text so the caller can confirm what was typed.
    """
    _dbg(f"send_message START handle={handle!r} text_len={len(text)}")
    if not text or not text.strip():
        raise ValueError("Message text must not be empty")
    text = text.strip()
    with _BROWSER_LOCK:
        pw = browser = context = None
        teardown = session is None
        if session is not None:
            context = session.context
        else:
            pw, browser, context = _connect_sync()
        before = {id(p) for p in context.pages}
        page = context.new_page()
        try:
            thread = _open_thread(context, page, str(handle).strip().lstrip("@"), timeout_ms)
            _human_wait(thread, 1400, 3400)  # read the last messages before replying
            focused = False
            for _ in range(6):
                try:
                    focused = bool(thread.evaluate(_FOCUS_COMPOSER_JS))
                except PlaywrightError:
                    focused = False
                if focused:
                    break
                _human_wait(thread, 450, 900)
            if not focused:
                raise RuntimeError(
                    f"Could not find the DM composer for @{handle}. X may require "
                    "them to have DMs open, or the page changed layout."
                )
            _human_wait(thread, 400, 900)
            _type_human(thread, text)
            _human_wait(thread, 700, 1600)  # pause, then re-check what was typed
            echoed = ""
            for _ in range(4):
                try:
                    echoed = str(thread.evaluate(_COMPOSER_TEXT_JS) or "").strip()
                except PlaywrightError:
                    echoed = ""
                if echoed:
                    break
                _human_wait(thread, 450, 900)
            _human_wait(thread, 600, 1300)  # look it over before the human sends
            _dbg(f"send_message @{handle} typed_len={len(echoed)} (send left to human)")
            return echoed
        finally:
            try:
                page.close()
            except Exception:
                pass
            _close_stray_pages(context, before)
            if teardown:
                browser.close()
                pw.stop()