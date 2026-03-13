"""
browser_session.py — Undetected Chrome via nodriver.

The browser is used ONLY to harvest cookies/tokens on first bootstrap or
when Cloudflare blocks curl_cffi (AuthExpiredError).
All actual job scraping is done by curl_cffi in fetchdata.py.

Install:
  pip install nodriver
  sudo apt install xvfb     # for invisible mode on Linux
"""

import asyncio
import json
import logging
import os
import random
import shutil
import subprocess
import time as _time
from pathlib import Path
import nodriver as uc
from database.database import log
import importlib.util

CONFIG_FILE = "config.json"
logger = logging.getLogger("browser_session")

_browser       = None
_xvfb_proc     = None
_xvfb_display  = None
_original_display = None


def _log(level: str, message: str):
    log(level, "browser_session", message)


def _patch_nodriver_encoding():
    try:
        spec = importlib.util.find_spec("nodriver")
        if not spec or not spec.submodule_search_locations:
            return

        base    = Path(spec.submodule_search_locations[0])
        cdp_dir = base / "cdp"
        if not cdp_dir.exists():
            return

        patched = False
        for py_file in cdp_dir.glob("*.py"):
            try:
                raw = py_file.read_bytes()
                try:
                    raw.decode("utf-8")
                    continue
                except UnicodeDecodeError:
                    pass

                text = raw.decode("latin-1")
                if not text.startswith("# -*- coding"):
                    text = "# -*- coding: utf-8 -*-\n" + text
                py_file.write_bytes(text.encode("utf-8"))
                logger.info(f"[patch] Fixed encoding in {py_file.name}")
                patched = True
            except (OSError, PermissionError) as e:
                logger.warning(f"[patch] Could not fix {py_file.name}: {e}")

        if patched:
            pycache = cdp_dir / "__pycache__"
            if pycache.exists():
                try:
                    shutil.rmtree(pycache)
                except OSError:
                    pass

    except (ImportError, OSError) as e:
        logger.warning(f"[patch] Could not patch nodriver encoding: {e}")


_patch_nodriver_encoding()


# ── Xvfb management ───────────────────────────────────────────────────────────

def _start_xvfb() -> str | None:
    """Start Xvfb. Returns the display string (e.g. ':870') or None if unavailable."""
    global _xvfb_proc, _xvfb_display

    if _xvfb_proc is not None and _xvfb_proc.poll() is None:
        return _xvfb_display

    try:
        if subprocess.run(["which", "Xvfb"], capture_output=True).returncode != 0:
            logger.info("[display] Xvfb not installed — browser will be visible.")
            return None
    except OSError:
        return None

    for _ in range(3):
        display_num = random.randint(100, 999)
        display_str = f":{display_num}"
        try:
            proc = subprocess.Popen(
                ["Xvfb", display_str, "-screen", "0", "1920x1080x24", "-nolisten", "tcp", "-ac"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _time.sleep(0.5)
            if proc.poll() is None:
                _xvfb_proc, _xvfb_display = proc, display_str
                logger.info(f"[display] Xvfb started on {display_str} (pid {proc.pid})")
                return display_str
        except OSError as e:
            logger.debug(f"[display] Xvfb attempt failed: {e}")

    logger.warning("[display] Could not start Xvfb after 3 attempts.")
    return None


def _stop_xvfb():
    global _xvfb_proc, _xvfb_display
    if _xvfb_proc is not None:
        try:
            _xvfb_proc.terminate()
            _xvfb_proc.wait(timeout=5)
        except OSError:
            try:
                _xvfb_proc.kill()
            except OSError:
                pass
        logger.info("[display] Xvfb stopped.")
        _xvfb_proc = _xvfb_display = None


# ── Browser launcher ──────────────────────────────────────────────────────────

BROWSER_ARGS_BASE = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--lang=en-US,en",
    "--window-size=1920,1080",
]


async def _launch_browser_hidden():
    global _original_display

    display = _start_xvfb()

    if display:
        _original_display = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = display
        wayland_display = os.environ.pop("WAYLAND_DISPLAY", None)
        xdg_session     = os.environ.get("XDG_SESSION_TYPE")
        if xdg_session:
            os.environ["XDG_SESSION_TYPE"] = "x11"

        logger.info(f"[browser] Launching Chrome on Xvfb {display} (X11 forced, invisible).")
        try:
            browser = await uc.start(
                headless=False,
                user_data_dir=os.path.abspath(".browser_profile"),
                browser_args=BROWSER_ARGS_BASE + ["--ozone-platform=x11", f"--display={display}"],
            )
        finally:
            if wayland_display is not None:
                os.environ["WAYLAND_DISPLAY"] = wayland_display
            if xdg_session:
                os.environ["XDG_SESSION_TYPE"] = xdg_session
    else:
        logger.info("[browser] No Xvfb — using --headless=new.")
        browser = await uc.start(
            headless=False,
            user_data_dir=os.path.abspath(".browser_profile"),
            browser_args=BROWSER_ARGS_BASE + ["--headless=new"],
        )

    return browser


def _restore_display():
    global _original_display
    if _original_display is not None:
        os.environ["DISPLAY"] = _original_display
        _original_display = None
    elif _xvfb_display and os.environ.get("DISPLAY") == _xvfb_display:
        os.environ.pop("DISPLAY", None)


# ── CF + extraction helpers ───────────────────────────────────────────────────

def _is_challenge_page(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in (
        "just a moment", "cloudflare", "attention required",
        "challenge", "please wait", "checking your browser", "verify you are human",
    ))


async def _wait_for_cloudflare(tab, timeout: int = 45) -> bool:
    start = asyncio.get_event_loop().time()
    while (asyncio.get_event_loop().time() - start) < timeout:
        try:
            title = await tab.evaluate("document.title")
            if not _is_challenge_page(title):
                logger.info(f"[CF] Challenge passed. Title: {title[:80]}")
                return True
        except Exception:
            pass

        try:
            await tab.mouse.move(random.randint(100, 900), random.randint(100, 600))
        except Exception:
            pass

        if random.random() < 0.15:
            try:
                await tab.mouse.click(random.randint(300, 700), random.randint(300, 500))
            except Exception:
                pass

        await asyncio.sleep(1.5 + random.random() * 2)

    logger.warning("[CF] Challenge did not resolve within timeout.")
    return False


async def _extract_cookies_and_token(browser, tab) -> tuple[dict, str | None]:
    cookies = {}

    try:
        raw = await browser.cookies.get_all()
        for c in raw:
            if "upwork.com" in (getattr(c, "domain", "") or ""):
                name  = getattr(c, "name", "")  or ""
                value = getattr(c, "value", "") or ""
                cookies[name] = value
    except Exception as e:
        logger.warning(f"[extract] Could not read cookies via CDP: {e}")
        try:
            js_cookies = await tab.evaluate("""
                document.cookie.split('; ').reduce((obj, pair) => {
                    const [k, ...v] = pair.split('=');
                    obj[k] = v.join('=');
                    return obj;
                }, {})
            """)
            if isinstance(js_cookies, dict):
                cookies.update(js_cookies)
        except Exception as e2:
            logger.warning(f"[extract] JS fallback failed: {e2}")

    token = None

    try:
        ls_raw = await tab.evaluate("""
            (() => {
                const out = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    out[k] = localStorage.getItem(k);
                }
                return out;
            })()
        """)
        if isinstance(ls_raw, dict):
            for v in ls_raw.values():
                if v and "oauth2v2" in str(v):
                    token = str(v).strip('"')
                    logger.info("[extract] Found token in localStorage.")
                    break
    except Exception as e:
        logger.warning(f"[extract] Could not read localStorage: {e}")

    if not token:
        for name in ("UniversalSearchNuxt_vt", "visitor_gql_token", "oauth2_global_js_token"):
            if cookies.get(name):
                token = cookies[name]
                logger.info(f"[extract] Found token in cookie: {name}")
                break

    return cookies, token


def _write_config(cookies: dict, token: str | None) -> None:
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {"COOKIES": {}}

    config.setdefault("COOKIES", {})
    config["COOKIES"].update(cookies)

    if token:
        for key in ("oauth2_global_js_token", "UniversalSearchNuxt_vt", "visitor_gql_token"):
            config["COOKIES"][key] = token

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)


# ── Shared harvest logic ──────────────────────────────────────────────────────

async def _harvest_cookies(cf_timeout: int = 60) -> bool:
    """
    Navigate to Upwork homepage + search page, harvest CF cookies and visitor tokens.
    Used by both bootstrap() (first run) and refresh_browser_cookies() (periodic).
    """
    global _browser

    try:
        _browser = await _launch_browser_hidden()

        tab = await _browser.get("https://www.upwork.com/")
        await _wait_for_cloudflare(tab, timeout=cf_timeout)
        await asyncio.sleep(3 + random.random() * 2)

        logger.info("[harvest] Navigating to search page for visitor tokens...")
        await tab.get("https://www.upwork.com/nx/search/jobs/?q=python&sort=recency")
        await _wait_for_cloudflare(tab, timeout=30)
        await asyncio.sleep(5 + random.random() * 3)

        cookies, token = await _extract_cookies_and_token(_browser, tab)
        if not cookies:
            logger.error("[harvest] No cookies harvested.")
            return False

        _write_config(cookies, token)
        logger.info(f"[harvest] Done — {len(cookies)} cookies, token={'yes' if token else 'no'}.")
        return True

    except Exception as e:
        logger.error(f"[harvest] Failed: {e}")
        return False
    finally:
        await _safe_close()


# ── Public API ────────────────────────────────────────────────────────────────

def needs_bootstrap() -> bool:
    """True if config.json is missing, empty, or lacks any visitor token."""
    if not os.path.exists(CONFIG_FILE):
        return True
    try:
        cookies = json.load(open(CONFIG_FILE)).get("COOKIES", {})
        if not cookies:
            return True
        visitor_tokens = ("UniversalSearchNuxt_vt", "visitor_gql_token", "oauth2_global_js_token")
        return not any(cookies.get(t) for t in visitor_tokens)
    except (json.JSONDecodeError, KeyError):
        return True


async def bootstrap() -> bool:
    """Bootstrap visitor session on first run (longer CF timeout)."""
    msg = "[bootstrap] Starting visitor cookie harvest..."
    logger.info(msg); _log("INFO", msg)
    ok = await _harvest_cookies(cf_timeout=60)
    if ok:
        _log("INFO", "[bootstrap] Success.")
    else:
        _log("ERROR", "[bootstrap] Failed.")
    return ok


async def refresh_browser_cookies() -> bool:
    """Periodic cookie refresh via hidden browser."""
    msg = "[refresh] Refreshing cookies via hidden browser..."
    logger.info(msg); _log("INFO", msg)
    ok = await _harvest_cookies(cf_timeout=45)
    if ok:
        _log("INFO", "[refresh] Cookies refreshed.")
    else:
        _log("WARNING", "[refresh] No cookies obtained.")
    return ok


async def _safe_close():
    global _browser
    if _browser:
        try:
            _browser.stop()
        except Exception:
            pass
        _browser = None
    _restore_display()
    _stop_xvfb()


async def close_session():
    await _safe_close()