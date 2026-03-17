"""
auth_manager.py — CF cookie refresh via HTTP (curl_cffi) for authenticated sessions.

CF cookies are refreshed every 25 min via a persistent curl_cffi session using
the stored authenticated cookies (master_access_token, user_uid, etc.).
The browser (nodriver) is only used on first bootstrap or when CF blocks curl_cffi,
triggered by AuthExpiredError in the scraper — not on a timer.
"""

import json
import logging
from datetime import datetime, timedelta

from curl_cffi import requests as cf_requests
from curl_cffi import CurlError

from database.database import log

CONFIG_FILE  = "config.json"
CF_LIFETIME  = timedelta(minutes=25)

logger           = logging.getLogger("auth_manager")
_last_cf_refresh = None

_cf_session = cf_requests.Session(impersonate="chrome")

# Cookies we want to keep fresh via the lightweight HTTP refresh
CF_COOKIES = {
    "__cf_bm", "_cfuvid", "cf_clearance",
    "AWSALB", "AWSALBCORS", "AWSALBTG", "AWSALBTGCORS",
    "__cflb", "spt", "forterToken",
}

# Auth cookies that must be present for a logged-in session
AUTH_COOKIE_NAMES = {"master_access_token", "user_uid", "oauth2_global_js_token"}


def _log(level: str, message: str):
    log(level, "auth_manager", message)


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(config: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)


def is_authenticated() -> bool:
    """Return True if config.json contains valid authenticated session cookies."""
    try:
        cookies = load_config().get("COOKIES", {})
        return any(cookies.get(name) for name in AUTH_COOKIE_NAMES)
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def should_refresh() -> bool:
    return _last_cf_refresh is None or (datetime.now() - _last_cf_refresh > CF_LIFETIME)


def refresh_cf_cookies() -> None:
    """
    Hit the Upwork homepage with curl_cffi using the stored authenticated cookies
    to obtain fresh CF cookies, then merge them back into config.json.
    """
    global _last_cf_refresh

    msg = "[refresh_cf] Refreshing Cloudflare cookies (authenticated session)..."
    logger.info(msg); _log("INFO", msg)

    config = load_config()
    stored_cookies = config.get("COOKIES", {})

    if not any(stored_cookies.get(name) for name in AUTH_COOKIE_NAMES):
        msg = "[refresh_cf] No auth cookies in config — skipping CF refresh (needs re-login)."
        logger.warning(msg); _log("WARNING", msg)
        return

    _cf_session.cookies.update(stored_cookies)

    try:
        _cf_session.get(
            "https://www.upwork.com/nx/find-work/best-matches",
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
            msg = f"[refresh_cf] Network error, no CF cookies obtained: {e}"
            logger.error(msg); _log("ERROR", msg)
            raise
        msg = f"[refresh_cf] Timed out but CF cookies present {got} — continuing."
        logger.warning(msg); _log("WARNING", msg)

    session_cookies = _cf_session.cookies.get_dict()

    # Only update CF cookies — never overwrite auth cookies with stale values
    updated = []
    for name in CF_COOKIES:
        if session_cookies.get(name):
            config["COOKIES"][name] = session_cookies[name]
            updated.append(name)

    save_config(config)
    _last_cf_refresh = datetime.now()
    msg = f"[refresh_cf] CF cookies refreshed: {updated}"
    logger.info(msg); _log("INFO", msg)