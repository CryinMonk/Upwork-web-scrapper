"""
browser_session.py — Undetected Chrome via nodriver.

The browser is used to:
  1. Bootstrap/refresh: check if session is alive, login only if actually needed,
     then do 2 searches via the search box to capture headers + cookies.

All actual job scraping is done by requests in fetchdata.py.

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

_browser          = None
_xvfb_proc        = None
_xvfb_display     = None
_original_display = None

AUTH_COOKIE_NAMES = {"master_access_token", "user_uid", "oauth2_global_js_token"}

_SEARCH_TERMS = ["shopify", "python", "wordpress", "react", "nodejs", "django"]


def _log(level: str, message: str):
    log(level, "browser_session", message)


# ── nodriver encoding patch ───────────────────────────────────────────────────

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


async def _launch_browser():
    """Launch Chrome reusing the persistent profile — never wipe it."""
    logger.info("[browser] Launching Chrome (reusing profile).")
    browser = await uc.start(
        headless=False,
        user_data_dir=os.path.abspath(".browser_profile"),
        browser_args=BROWSER_ARGS_BASE,
    )
    return browser

# async def _launch_browser():
#     """Launch Chrome invisibly via Xvfb, falling back to --headless=new if Xvfb unavailable."""
#     global _original_display
#
#     display = _start_xvfb()
#
#     if display:
#         _original_display = os.environ.get("DISPLAY")
#         os.environ["DISPLAY"] = display
#         wayland_display = os.environ.pop("WAYLAND_DISPLAY", None)
#         xdg_session     = os.environ.get("XDG_SESSION_TYPE")
#         if xdg_session:
#             os.environ["XDG_SESSION_TYPE"] = "x11"
#
#         logger.info(f"[browser] Launching Chrome on Xvfb {display} (invisible).")
#         try:
#             browser = await uc.start(
#                 headless=False,
#                 user_data_dir=os.path.abspath(".browser_profile"),
#                 browser_args=BROWSER_ARGS_BASE + ["--ozone-platform=x11", f"--display={display}"],
#             )
#         finally:
#             if wayland_display is not None:
#                 os.environ["WAYLAND_DISPLAY"] = wayland_display
#             if xdg_session:
#                 os.environ["XDG_SESSION_TYPE"] = xdg_session
#     else:
#         logger.info("[browser] No Xvfb — using --headless=new.")
#         browser = await uc.start(
#             headless=False,
#             user_data_dir=os.path.abspath(".browser_profile"),
#             browser_args=BROWSER_ARGS_BASE + ["--headless=new"],
#         )
#
#     return browser


def _restore_display():
    global _original_display
    if _original_display is not None:
        os.environ["DISPLAY"] = _original_display
        _original_display = None
    elif _xvfb_display and os.environ.get("DISPLAY") == _xvfb_display:
        os.environ.pop("DISPLAY", None)


# ── CF challenge helper ───────────────────────────────────────────────────────

def _is_challenge_page(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in (
        "just a moment", "cloudflare", "attention required",
        "challenge", "please wait", "checking your browser", "verify you are human",
    ))


def _is_login_url(url: str) -> bool:
    return "login" in url.lower() or "accounts.upwork.com" in url.lower()


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


# ── Session check ─────────────────────────────────────────────────────────────

async def _check_session(tab) -> bool:
    """
    Navigate to find-work and check if we land on a logged-in page.
    Returns True if the session is alive (no redirect to login).
    """
    try:
        await tab.get("https://www.upwork.com/nx/find-work/")
        await _wait_for_cloudflare(tab, timeout=30)
        await asyncio.sleep(2)
        url = await tab.evaluate("window.location.href")
        logged_in = not _is_login_url(url)
        logger.info(
            f"[session] {'Valid — skipping login' if logged_in else 'Expired — login needed'}. "
            f"URL: {url[:80]}"
        )
        return logged_in
    except Exception as e:
        logger.warning(f"[session] Check failed: {e}")
        return False


# ── Login form automation ─────────────────────────────────────────────────────

async def _select_first(tab, *selectors: str, timeout: int = 15):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for sel in selectors:
            try:
                el = await tab.select(sel, timeout=1)
                if el:
                    return el
            except Exception:
                pass
        await asyncio.sleep(0.5)
    raise RuntimeError(f"None of {selectors} found within {timeout}s")


async def _dump_page_html(tab, label: str):
    try:
        html = await tab.evaluate("document.body.outerHTML")
        Path("login_debug.html").write_text(html or "", encoding="utf-8")
        logger.info(f"[{label}] Page HTML written to login_debug.html")
    except Exception as e:
        logger.warning(f"[{label}] Could not dump HTML: {e}")


async def _do_login(tab, username: str, password: str, cf_timeout: int = 60) -> bool:
    """
    Navigate to the login page and submit credentials.
    Handles the case where the profile's session is still alive and Upwork
    redirects away from the login page instead of showing the form —
    in that case we're already logged in and return True immediately.
    """
    await tab.get("https://www.upwork.com/ab/account-security/login")
    await _wait_for_cloudflare(tab, timeout=cf_timeout)
    await asyncio.sleep(2 + random.random() * 2)

    # Check where we actually landed — if Upwork redirected us away from
    # the login page, the profile session is still valid.
    url = await tab.evaluate("window.location.href")
    if not _is_login_url(url):
        logger.info(
            f"[login] Navigated to login URL but landed on {url[:80]} — "
            f"session is still alive, skipping form."
        )
        return True

    # We're on the login page — fill the form
    try:
        email_input = await _select_first(
            tab,
            "input#login_username",
            "input[name='login[username]']",
            "input[autocomplete='username']",
            "input[type='email']",
            timeout=15,
        )
        await email_input.clear_input()
        await email_input.send_keys(username)
        await asyncio.sleep(0.6 + random.random() * 0.6)
    except Exception as e:
        logger.error(f"[login] Email input not found: {e}")
        await _dump_page_html(tab, "email-step")
        return False

    try:
        continue_btn = await _select_first(
            tab,
            "button[data-qa='btn-auth-login-continue']",
            "button[data-test='btn-auth-login-continue']",
            "#login_password_continue",
            "button[type='submit']",
            timeout=10,
        )
        await continue_btn.click()
        logger.info("[login] Email submitted.")
        await asyncio.sleep(2 + random.random())
    except Exception as e:
        logger.error(f"[login] Continue button not found: {e}")
        await _dump_page_html(tab, "continue-btn")
        return False

    await _wait_for_cloudflare(tab, timeout=20)

    try:
        pw_input = await _select_first(
            tab,
            "input#login_password",
            "input[name='login[password]']",
            "input[autocomplete='current-password']",
            "input[type='password']",
            timeout=15,
        )
        await pw_input.clear_input()
        await pw_input.send_keys(password)
        await asyncio.sleep(0.6 + random.random() * 0.6)
    except Exception as e:
        logger.error(f"[login] Password input not found: {e}")
        await _dump_page_html(tab, "password-step")
        return False

    try:
        keep = await _select_first(
            tab,
            "input#login_rememberme",
            "input[name='login[rememberme]']",
            "input[type='checkbox']",
            timeout=5,
        )
        is_checked = await tab.evaluate(
            "document.querySelector(\"input#login_rememberme, input[type='checkbox']\")?.checked"
        )
        if not is_checked:
            await keep.click()
            logger.info("[login] Checked 'Keep me logged in'.")
    except Exception:
        logger.info("[login] 'Keep me logged in' not found — skipping.")

    try:
        login_btn = await _select_first(
            tab,
            "button[data-qa='btn-auth-login-login']",
            "button[data-test='btn-auth-login-login']",
            "#login_control_continue",
            "button[type='submit']",
            timeout=10,
        )
        await login_btn.click()
        logger.info("[login] Password submitted — waiting for redirect...")
    except Exception as e:
        logger.error(f"[login] Log In button not found: {e}")
        await _dump_page_html(tab, "login-btn")
        return False

    # Wait for post-login redirect
    start = asyncio.get_event_loop().time()
    while (asyncio.get_event_loop().time() - start) < 60:
        try:
            url   = await tab.evaluate("window.location.href")
            title = await tab.evaluate("document.title")
            if _is_challenge_page(title):
                await asyncio.sleep(2)
                continue
            if _is_login_url(url):
                try:
                    err = await tab.evaluate(
                        "document.querySelector('[data-qa=\"alert-error\"]')?.innerText || ''"
                    )
                    if err.strip():
                        logger.error(f"[login] Error: {err.strip()}")
                        return False
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue
            logger.info(f"[login] Logged in. URL: {url[:100]}")
            return True
        except Exception:
            await asyncio.sleep(1)

    logger.warning("[login] Timed out waiting for post-login redirect.")
    return False


# ── Cookie extraction ─────────────────────────────────────────────────────────

async def _extract_all_cookies(browser, tab) -> dict:
    cookies = {}
    try:
        result = await tab.send(uc.cdp.network.get_all_cookies())
        all_cookies = result if isinstance(result, list) else getattr(result, "cookies", [])
        for c in all_cookies:
            domain = getattr(c, "domain", "") or ""
            name   = getattr(c, "name",  "") or ""
            value  = getattr(c, "value", "") or ""
            if "upwork.com" in domain and name:
                cookies[name] = value
        logger.info(f"[extract] {len(cookies)} cookies. Auth: {AUTH_COOKIE_NAMES & set(cookies)}")
    except Exception as e:
        logger.warning(f"[extract] CDP failed: {e} — trying fallback")
        try:
            raw = await browser.cookies.get_all()
            for c in raw:
                domain = getattr(c, "domain", "") or ""
                name   = getattr(c, "name",  "") or ""
                value  = getattr(c, "value", "") or ""
                if "upwork.com" in domain and name:
                    cookies[name] = value
        except Exception as e2:
            logger.warning(f"[extract] Fallback failed: {e2}")
    return cookies


# ── Search box helpers ────────────────────────────────────────────────────────

_SEARCH_INPUT_SELECTORS = [
    "input[data-test='search-input']",
    "input[placeholder*='Search']",
    "input[placeholder*='search']",
    "input[aria-label*='Search']",
    "input[aria-label*='search']",
    "input#search-input",
    "input.search-input",
    "[data-qa='search-input'] input",
    "header input[type='text']",
    "nav input[type='text']",
    "input[type='search']",
    "input[type='text']",
]


async def _find_search_input(tab):
    for sel in _SEARCH_INPUT_SELECTORS:
        try:
            el = await tab.select(sel, timeout=2)
            if el:
                logger.info(f"[search] Found input via: {sel!r}")
                return el, sel
        except Exception:
            pass
    return None, None


async def _type_and_submit(tab, term: str) -> bool:
    """Find the search box, type term, press Enter. Returns True on success."""
    el, sel = await _find_search_input(tab)

    if el is None:
        logger.warning(f"[search] Input not found for {term!r} — trying JS fallback")
        try:
            await tab.evaluate("""
                const inputs = [...document.querySelectorAll(
                    'input[type="text"], input[type="search"], input:not([type])'
                )];
                const visible = inputs.find(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && r.top < 200;
                });
                if (visible) { visible.focus(); visible.value = ''; }
            """)
            await asyncio.sleep(0.5)
            await tab.keyboard.send_keys(term)
            await asyncio.sleep(0.5 + random.random() * 0.3)
            await tab.keyboard.send_keys("\n")
            logger.info(f"[search] Submitted {term!r} via JS fallback.")
            return True
        except Exception as e:
            logger.warning(f"[search] JS fallback failed: {e}")
            return False

    try:
        await el.click()
        await asyncio.sleep(0.3)
    except Exception:
        pass

    # Clear via JS then type fresh
    try:
        await tab.evaluate(f"const el = document.querySelector({json.dumps(sel)}); if(el) el.value = '';")
    except Exception:
        pass

    await el.send_keys(term)
    await asyncio.sleep(0.8 + random.random() * 0.5)
    await el.send_keys("\n")
    logger.info(f"[search] Submitted {term!r}.")
    return True


# ── Header capture via 2 searches ────────────────────────────────────────────

async def _capture_headers_via_two_searches(tab) -> dict:
    """
    Perform 2 searches using the search box to reliably trigger userJobSearch.

    Search 1 — from find-work/best-matches page, primes the search surface.
    Search 2 — a different term typed into the now-loaded search box.
                This reliably fires userJobSearch on the second interaction.

    Headers from the LAST captured GraphQL request are saved (search 2).
    """
    terms = random.sample(_SEARCH_TERMS, k=2)
    all_captured: list[dict] = []

    async def _on_request(event: uc.cdp.network.RequestWillBeSent):
        url = getattr(event.request, "url", "") or ""
        if "api/graphql" in url and len(all_captured) < 2:
            headers: dict = {}
            raw = event.request.headers
            if hasattr(raw, "items"):
                headers.update({k.lower(): v for k, v in raw.items()})
            elif isinstance(raw, dict):
                headers.update({k.lower(): v for k, v in raw.items()})
            if headers:
                alias = url.split("alias=")[-1] if "alias=" in url else "unknown"
                all_captured.append(headers)
                logger.info(
                    f"[capture] Request #{len(all_captured)}: alias={alias}  "
                    f"authorization: {headers.get('authorization', 'MISSING')[:40]!r}  "
                    f"tenant: {headers.get('x-upwork-api-tenantid', 'MISSING')!r}"
                )

    try:
        await tab.send(uc.cdp.network.enable())
        tab.add_handler(uc.cdp.network.RequestWillBeSent, _on_request)

        # ── Search 1 ───────────────────────────────────────────────────────
        logger.info(f"[capture] Search 1: navigating to find-work, typing {terms[0]!r}...")
        await tab.get("https://www.upwork.com/nx/find-work/best-matches")
        await _wait_for_cloudflare(tab, timeout=30)
        await asyncio.sleep(3 + random.random() * 2)

        await _type_and_submit(tab, terms[0])
        await _wait_for_cloudflare(tab, timeout=20)
        await asyncio.sleep(3 + random.random() * 2)
        logger.info(f"[capture] After search 1: {len(all_captured)} request(s) captured.")

        # ── Search 2 ───────────────────────────────────────────────────────
        logger.info(f"[capture] Search 2: typing {terms[1]!r}...")
        await _type_and_submit(tab, terms[1])
        await _wait_for_cloudflare(tab, timeout=20)

        # Wait up to 10s for the second request
        for _ in range(20):
            if len(all_captured) >= 2:
                break
            await asyncio.sleep(0.5)

        logger.info(f"[capture] After search 2: {len(all_captured)} request(s) captured.")
        tab.remove_handler(uc.cdp.network.RequestWillBeSent, _on_request)

    except Exception as e:
        logger.warning(f"[capture] Failed: {e}")
        try:
            tab.remove_handler(uc.cdp.network.RequestWillBeSent, _on_request)
        except Exception:
            pass

    if not all_captured:
        logger.warning("[capture] No GraphQL requests intercepted.")
        return {}

    final = all_captured[-1]
    logger.info(
        f"[capture] Using headers from request #{len(all_captured)}. "
        f"keys: {sorted(final.keys())}"
    )
    return final


def _write_config(cookies: dict, headers: dict | None = None) -> None:
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}

    config["COOKIES"] = cookies
    if headers:
        _skip = {"cookie", "content-length", "transfer-encoding", "connection"}
        config["HEADERS"] = {k: v for k, v in headers.items() if k not in _skip}

    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

    logger.info(
        f"[write_config] Wrote {len(cookies)} cookies"
        + (f" and {len(config.get('HEADERS', {}))} headers" if headers else "")
        + f" to {CONFIG_FILE!r}"
    )


def _load_credentials() -> tuple[str, str]:
    try:
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        username = config.get("USERNAME", "").strip()
        password = config.get("PASSWORD", "").strip()
    except (FileNotFoundError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Cannot read {CONFIG_FILE}: {e}") from e
    if not username or not password:
        raise RuntimeError(f"USERNAME and PASSWORD must be set in {CONFIG_FILE}.")
    return username, password


# ── Core harvest flow ─────────────────────────────────────────────────────────

async def _harvest(force_login: bool = False, cf_timeout: int = 60) -> bool:
    """
    1. Launch Chrome reusing persistent profile.
    2. Determine if login is needed:
       - force_login=False: do a quick session check (find-work/ redirect test).
       - force_login=True:  navigate to the login URL and fill the form IF we
                            actually land on the login page. If Upwork redirects
                            us to the homepage instead, the session is still alive
                            and we skip the form — no error.
    3. Do 2 searches via the search box to capture GraphQL headers.
    4. Extract all cookies, write to config.json.
    """
    global _browser

    try:
        _browser = await _launch_browser()
        tab = await _browser.get("about:blank")

        # ── Determine if we need to log in ────────────────────────────────
        need_login = False

        if force_login:
            # Navigate to login URL — _do_login handles the redirect-to-homepage
            # case gracefully and returns True without touching the form.
            logger.info("[harvest] force_login=True — checking login page...")
            username, password = _load_credentials()
            ok = await _do_login(tab, username, password, cf_timeout=cf_timeout)
            if not ok:
                logger.error("[harvest] Login failed.")
                return False
        else:
            # Quick session check
            already_logged_in = await _check_session(tab)
            if not already_logged_in:
                logger.info("[harvest] Session expired — logging in...")
                username, password = _load_credentials()
                ok = await _do_login(tab, username, password, cf_timeout=cf_timeout)
                if not ok:
                    logger.error("[harvest] Login failed.")
                    return False
            else:
                logger.info("[harvest] Session valid — skipping login.")

        # ── 2 searches to capture headers ─────────────────────────────────
        logger.info("[harvest] Starting 2-search header capture...")
        headers = await _capture_headers_via_two_searches(tab)

        if not headers:
            logger.error("[harvest] Could not capture headers.")
            return False

        # ── Extract cookies ────────────────────────────────────────────────
        await asyncio.sleep(1 + random.random())
        cookies = await _extract_all_cookies(_browser, tab)

        if not cookies:
            logger.error("[harvest] No cookies extracted.")
            return False

        got_auth = AUTH_COOKIE_NAMES & set(cookies)
        if not got_auth:
            logger.error(f"[harvest] Auth cookies missing. Got: {list(cookies)[:10]}")
            return False

        _write_config(cookies, headers=headers)

        logger.info(
            f"[harvest] Success — {len(cookies)} cookies, {len(headers)} headers. "
            f"Auth: {got_auth}  "
            f"authorization: {headers.get('authorization', 'MISSING')[:40]!r}  "
            f"tenant: {headers.get('x-upwork-api-tenantid', 'MISSING')!r}"
        )
        _log("INFO", f"[harvest] Done. Auth: {got_auth}")
        return True

    except Exception as e:
        logger.error(f"[harvest] Unexpected error: {e}")
        _log("ERROR", f"[harvest] Failed: {e}")
        return False
    finally:
        await _safe_close()


# ── Public API ────────────────────────────────────────────────────────────────

def needs_bootstrap() -> bool:
    if not os.path.exists(CONFIG_FILE):
        return True
    try:
        config  = json.load(open(CONFIG_FILE))
        cookies = config.get("COOKIES", {})
        if not cookies:
            return True
        return not any(cookies.get(name) for name in AUTH_COOKIE_NAMES)
    except (json.JSONDecodeError, KeyError):
        return True


async def bootstrap() -> bool:
    """First run — reuse session if alive, log in only if needed, then capture."""
    msg = "[bootstrap] Starting..."
    logger.info(msg); _log("INFO", msg)
    ok = await _harvest(force_login=False, cf_timeout=60)
    if ok:
        _log("INFO", "[bootstrap] Done.")
    else:
        _log("ERROR", "[bootstrap] Failed — check credentials in config.json.")
    return ok


async def refresh_browser_cookies() -> bool:
    """
    Called when AuthExpiredError fires. Navigates to the login URL:
    - If redirected to homepage → session is still alive, just redo the search capture.
    - If login form appears → fill credentials and log in fresh.
    """
    msg = "[refresh] Re-authenticating..."
    logger.info(msg); _log("INFO", msg)
    ok = await _harvest(force_login=True, cf_timeout=45)
    if ok:
        _log("INFO", "[refresh] Done.")
    else:
        _log("WARNING", "[refresh] Failed.")
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