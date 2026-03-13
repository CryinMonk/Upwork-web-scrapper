import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from utilties.json_helper import get_json
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database.database import (
    init_db, is_job_posted, mark_job_posted, get_active_search_channels,
    cleanup_old_jobs, cleanup_old_logs, log,
)
from fetch_data.fetchdata import fetch_jobs_with_details, AuthExpiredError
from curl_cffi import CurlError
from discord_post_and_formatter.helpers import build_embed
from discord_post_and_formatter.thread_poster import post_job_thread
from utilties.memory import check_memory
from auth_and_browser.auth_manager import should_refresh, refresh_cf_cookies
from auth_and_browser.browser_session import bootstrap, needs_bootstrap, close_session, refresh_browser_cookies
from discord_bot_commands.commands import register_commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("discordbot")

load_dotenv()

DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 5))

MAX_RETRIES   = int(get_json()["Retry"]["MAX_RETRIES"])
RETRY_DELAY   = int(get_json()["Retry"]["RETRY_DELAY"])
BACKOFF_DELAY = int(get_json()["Retry"]["BACKOFF_DELAY"])

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Stored on bot so commands can read them in !status
bot.start_time:        datetime | None = None
bot.last_refresh_time: datetime | None = None


def _log(level: str, message: str):
    log(level, "discordbot", message)


# ─── Retry helper ─────────────────────────────────────────────────────────────

async def with_retry(label: str, coro_fn, *args, **kwargs):
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except (discord.HTTPException, discord.GatewayNotFound,
                discord.ConnectionClosed, OSError) as e:
            last_exc = e
            msg = f"[{label}] {type(e).__name__} (attempt {attempt}/{MAX_RETRIES}): {e}"
            logger.warning(msg); _log("WARNING", msg)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)

    msg = f"[{label}] All {MAX_RETRIES} retries exhausted. Backing off for {BACKOFF_DELAY}s."
    logger.error(msg); _log("ERROR", msg)
    await asyncio.sleep(BACKOFF_DELAY)
    raise last_exc


# ─── Bot events ───────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    if bot.start_time is None:
        bot.start_time = datetime.now(tz=timezone.utc)
        init_db()
        register_commands(bot)
        if needs_bootstrap():
            logger.info("No session found — running one-time browser login...")
            _log("INFO", "No session found — running one-time browser login...")
            if not await bootstrap():
                logger.error("Bootstrap failed — cannot start scraper. Exiting.")
                _log("ERROR", "Bootstrap failed — cannot start scraper. Exiting.")
                await bot.close()
                return
        job_scraper_loop.start()
    logger.info("Bot logged in as %s", bot.user)
    _log("INFO", f"Bot logged in as {bot.user}")


@bot.event
async def on_disconnect():
    msg = "Bot disconnected from Discord. Will attempt to reconnect automatically."
    logger.warning(msg); _log("WARNING", msg)


@bot.event
async def on_resumed():
    msg = "Bot session resumed successfully."
    logger.info(msg); _log("INFO", msg)


@bot.event
async def on_error(event, *args, **kwargs):
    logger.exception("Unhandled error in event '%s'", event)
    _log("ERROR", f"Unhandled error in event '{event}'")


# ─── Scraper helpers ──────────────────────────────────────────────────────────

async def _do_housekeeping() -> None:
    check_memory()
    try:
        cleanup_old_jobs()
        cleanup_old_logs()
    except sqlite3.Error as e:
        msg = f"[Scraper] DB cleanup failed (non-fatal): {e}"
        logger.warning(msg); _log("WARNING", msg)


async def _refresh_cf_if_due() -> None:
    if not should_refresh():
        return
    logger.info("[Scraper] CF refresh due — refreshing...")
    _log("INFO", "[Scraper] CF refresh due — refreshing...")
    try:
        await asyncio.to_thread(refresh_cf_cookies)
        bot.last_refresh_time = datetime.now(tz=timezone.utc)
        logger.info("[Scraper] CF cookies refreshed successfully.")
        _log("INFO", "[Scraper] CF cookies refreshed successfully.")
    except CurlError as e:
        msg = f"[Scraper] Could not refresh CF cookies: {e}. Continuing with existing cookies."
        logger.warning(msg); _log("WARNING", msg)


async def _fetch_with_auth_retry(keyword: str) -> list[dict] | None:
    async def _fetch():
        return await asyncio.to_thread(fetch_jobs_with_details, query=keyword, count=10)

    try:
        return await with_retry(f"Fetch:{keyword}", _fetch)
    except AuthExpiredError:
        msg = f"[Scraper] Auth expired fetching '{keyword}'. Running browser cookie refresh..."
        logger.warning(msg); _log("WARNING", msg)
        try:
            ok = await refresh_browser_cookies()
            if not ok:
                raise RuntimeError("Browser cookie harvest returned no cookies.")
            bot.last_refresh_time = datetime.now(tz=timezone.utc)
            return await with_retry(f"Fetch:{keyword}:post-refresh", _fetch)
        except (AuthExpiredError, RuntimeError, CurlError) as e:
            msg = f"[Scraper] Still failing for '{keyword}' after browser refresh: {e}. Skipping."
            logger.error(msg); _log("ERROR", msg)
            return None
    except (AuthExpiredError, RuntimeError, CurlError) as e:
        msg = f"[Scraper] Failed to fetch jobs for '{keyword}' after all retries: {e}."
        logger.error(msg); _log("ERROR", msg)
        return None


async def _post_job(channel: discord.TextChannel, job_id: str, details: dict) -> bool:
    try:
        if is_job_posted(job_id):
            return False
    except sqlite3.Error as e:
        msg = f"[Scraper] DB error checking job {job_id}: {e}. Skipping."
        logger.error(msg); _log("ERROR", msg)
        return False

    try:
        message = await with_retry(
            f"SendEmbed:{job_id}",
            lambda: channel.send(embed=build_embed(details)),
        )
    except discord.DiscordException as e:
        msg = f"[Scraper] Failed to send embed for job {job_id}: {e}. Skipping."
        logger.error(msg); _log("ERROR", msg)
        return False

    try:
        await with_retry(
            f"PostThread:{job_id}",
            lambda: post_job_thread(message, details),
        )
    except discord.DiscordException as e:
        msg = f"[Scraper] Failed to post thread for {job_id}: {e}. Embed was still sent."
        logger.error(msg); _log("ERROR", msg)

    try:
        mark_job_posted(job_id, details)
    except sqlite3.Error as e:
        msg = f"[Scraper] DB error marking job {job_id} as posted: {e}."
        logger.error(msg); _log("ERROR", msg)

    return True


# ─── Scraper loop ─────────────────────────────────────────────────────────────

@tasks.loop(minutes=CHECK_INTERVAL)
async def job_scraper_loop():
    await _do_housekeeping()
    await _refresh_cf_if_due()

    try:
        search_channels = get_active_search_channels()
    except sqlite3.Error as e:
        msg = f"[Scraper] Failed to read search channels: {e}. Skipping cycle."
        logger.error(msg); _log("ERROR", msg)
        return

    if not search_channels:
        logger.info("[Scraper] No active keyword→channel mappings found.")
        return

    msg = f"[Scraper] Running for {len(search_channels)} keyword(s)..."
    logger.info(msg); _log("INFO", msg)

    for entry in search_channels:
        keyword = entry["keyword"]
        channel = bot.get_channel(int(entry["channel_id"]))

        if not channel:
            msg = f"[Scraper] Channel {entry['channel_id']} not found for '{keyword}' — skipping."
            logger.warning(msg); _log("WARNING", msg)
            continue

        jobs = await _fetch_with_auth_retry(keyword)
        if jobs is None:
            continue

        new_count = 0
        for details in jobs:
            job_id = details.get("_ciphertext")
            if not job_id:
                msg = f"[Scraper] Missing ciphertext under '{keyword}', skipping."
                logger.warning(msg); _log("WARNING", msg)
                continue
            if await _post_job(channel, job_id, details):
                new_count += 1
                await asyncio.sleep(1)

        msg = f"  └─ '{keyword}' → #{channel.name}: {new_count} new job(s)"
        logger.info(msg); _log("INFO", msg)


@job_scraper_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()


@job_scraper_loop.error
async def scraper_loop_error(error):
    msg = f"[Scraper] Unhandled exception — will restart on next tick: {error}"
    logger.exception(msg); _log("ERROR", msg)


# ─── Shutdown ─────────────────────────────────────────────────────────────────

@bot.event
async def on_close():
    await close_session()


def close_bot():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(close_session())
            loop.create_task(bot.close())
        else:
            loop.run_until_complete(close_session())
            loop.run_until_complete(bot.close())
        logger.info("Bot closed cleanly.")
    except RuntimeError as e:
        logger.error("Error during bot shutdown: %s", e)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)