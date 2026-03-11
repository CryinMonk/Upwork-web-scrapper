"""
auth_manager.py — Token refresh via headless browser + CF cookies via HTTP.

After browser_session.bootstrap() runs once:
  - CF cookies refreshed every 25 min via curl_cffi (fast, no browser)
  - Auth token refreshed every 11 hours via nodriver (loads homepage, grabs token)
"""


import json
import logging
from datetime import datetime, timedelta
from curl_cffi import requests as cf_requests
from curl_cffi import CurlError
from browser_session import refresh_browser_cookies
from database import log

CONFIG_FILE   = "config.json"
CF_LIFETIME   = timedelta(minutes=25)
AUTH_LIFETIME = timedelta(hours=11)

logger             = logging.getLogger("auth_manager")
_last_cf_refresh   = None
_last_auth_refresh = None

# Session — reused across all CF refreshes
_cf_session = cf_requests.Session(impersonate="chrome")

CF_COOKIES = {
    "__cf_bm", "_cfuvid", "cf_clearance",
    "AWSALB", "AWSALBCORS", "AWSALBTG", "AWSALBTGCORS",
    "__cflb", "spt", "forterToken",
}
AUTH_TOKENS = {"oauth2_global_js_token", "UniversalSearchNuxt_vt", "visitor_gql_token"}


def _log(level: str, message: str):
    log(level, "auth_manager", message)


def load_config(path: str = CONFIG_FILE) -> dict:
    with open(path) as f:
        return json.load(f)


def save_config(config: dict, path: str = CONFIG_FILE):
    with open(path, "w") as f:
        json.dump(config, f, indent=4)


def get_cookies_and_headers(path: str = CONFIG_FILE) -> tuple[dict, dict]:
    config = load_config(path)
    return config["COOKIES"], config["HEADERS"]


def should_refresh() -> bool:
    return _last_cf_refresh is None or (datetime.now() - _last_cf_refresh > CF_LIFETIME)


def should_refresh_auth() -> bool:
    return _last_auth_refresh is None or (datetime.now() - _last_auth_refresh > AUTH_LIFETIME)


# ── CF cookie refresh (HTTP only, persistent session) ─────────────────────────

def refresh_cf_cookies() -> None:
    """Hit Upwork homepage with curl_cffi to get fresh CF cookies."""
    global _last_cf_refresh

    msg = "[refresh_cf] Refreshing Cloudflare cookies..."
    logger.info(msg); _log("INFO", msg)

    config = load_config()
    _cf_session.cookies.update(config["COOKIES"])

    try:
        _cf_session.get(
            "https://www.upwork.com/",
            headers={
                "user-agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
                "accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language": "en-US,en;q=0.9",
            },
            timeout=15,
        )
    except CurlError as e:
        got = {k for k in _cf_session.cookies.get_dict() if k in CF_COOKIES}
        if not got:
            msg = f"[refresh_cf] Network error, no CF cookies: {e}"
            logger.error(msg); _log("ERROR", msg)
            raise
        msg = f"[refresh_cf] Timed out but CF cookies present {got} — continuing."
        logger.warning(msg); _log("WARNING", msg)

    session_cookies = _cf_session.cookies.get_dict()
    updated = []
    for name in CF_COOKIES:
        val = session_cookies.get(name)
        if val:
            config["COOKIES"][name] = val
            updated.append(name)

    save_config(config)
    _last_cf_refresh = datetime.now()
    msg = f"[refresh_cf] CF cookies refreshed: {updated}"
    logger.info(msg); _log("INFO", msg)


# ── Auth token refresh (nodriver browser) ─────────────────────────────────────

async def refresh_auth_tokens() -> None:
    """
    Refresh visitor cookies by launching an undetected browser.
    Delegates entirely to browser_session.refresh_browser_cookies().
    """
    global _last_auth_refresh

    msg = "[refresh_auth] Refreshing visitor cookies via nodriver browser..."
    logger.info(msg); _log("INFO", msg)

    ok = await refresh_browser_cookies()
    if ok:
        _last_auth_refresh = datetime.now()
    else:
        raise RuntimeError("[refresh_auth] Failed to harvest visitor cookies.")


# ── Full refresh ──────────────────────────────────────────────────────────────

async def full_refresh() -> None:
    """Refresh CF cookies (HTTP), then auth token via browser if due."""
    refresh_cf_cookies()
    if should_refresh_auth():
        await refresh_auth_tokens()

