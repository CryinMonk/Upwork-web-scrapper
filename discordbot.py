import os
import asyncio
import logging
import psutil
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import (
    init_db, is_job_posted, mark_job_posted, get_active_search_channels,
    cleanup_old_jobs, cleanup_old_logs, log,
    count_recent_jobs, count_recent_errors, remove_search_channel, add_search_channel
)
from fetchdata import fetch_jobs_with_details, AuthExpiredError
from helpers import build_embed
from thread_poster import post_job_thread
from memory import check_memory
from auth_manager import should_refresh, should_refresh_auth, full_refresh
from browser_session import bootstrap, needs_bootstrap, close_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("discordbot")


def log_to_db(level: str, message: str):
    log(level, "discordbot", message)


load_dotenv()

DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 5))

MAX_RETRIES   = 3
RETRY_DELAY   = 10
BACKOFF_DELAY = 300

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

BOT_START_TIME: datetime | None = None
_last_refresh_time: datetime | None = None


# ─── Retry helper ─────────────────────────────────────────────────────────────

async def with_retry(label: str, coro_fn, *args, **kwargs):
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except discord.HTTPException as e:
            last_exc = e
            msg = f"[{label}] Discord HTTP error (attempt {attempt}/{MAX_RETRIES}): status={e.status} — {e.text}"
        except discord.GatewayNotFound as e:
            last_exc = e
            msg = f"[{label}] Discord gateway not found (attempt {attempt}/{MAX_RETRIES}): {e}"
        except discord.ConnectionClosed as e:
            last_exc = e
            msg = f"[{label}] Discord connection closed (attempt {attempt}/{MAX_RETRIES}): code={e.code}"
        except OSError as e:
            last_exc = e
            msg = f"[{label}] Network/OS error (attempt {attempt}/{MAX_RETRIES}): {e}"
        except Exception as e:
            last_exc = e
            msg = f"[{label}] Unexpected error (attempt {attempt}/{MAX_RETRIES}): {type(e).__name__}: {e}"

        logger.warning(msg)
        log_to_db("WARNING", msg)

        if attempt < MAX_RETRIES:
            retry_msg = f"[{label}] Retrying in {RETRY_DELAY}s..."
            logger.info(retry_msg)
            log_to_db("INFO", retry_msg)
            await asyncio.sleep(RETRY_DELAY)

    exhausted_msg = f"[{label}] All {MAX_RETRIES} retries exhausted. Backing off for {BACKOFF_DELAY}s (5 min)."
    logger.error(exhausted_msg)
    log_to_db("ERROR", exhausted_msg)
    await asyncio.sleep(BACKOFF_DELAY)
    raise last_exc


# ─── Auth client refresh with retry ──────────────────────────────────────────

async def refresh_client():
    global _last_refresh_time

    async def _do_refresh():
        await full_refresh()

    await with_retry("AuthRefresh", _do_refresh)
    _last_refresh_time = datetime.now(tz=timezone.utc)
    return True


# ─── Bot events ───────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global BOT_START_TIME
    if BOT_START_TIME is None:
        BOT_START_TIME = datetime.now(tz=timezone.utc)
        init_db()
        if needs_bootstrap():
            logger.info("No session found — running one-time browser login...")
            log_to_db("INFO", "No session found — running one-time browser login...")
            ok = await bootstrap()
            if not ok:
                logger.error("Bootstrap failed — cannot start scraper. Exiting.")
                log_to_db("ERROR", "Bootstrap failed — cannot start scraper. Exiting.")
                await bot.close()
                return
        job_scraper_loop.start()
    logger.info("Bot logged in as %s", bot.user)
    log_to_db("INFO", f"Bot logged in as {bot.user}")


@bot.event
async def on_disconnect():
    logger.warning("Bot disconnected from Discord. Will attempt to reconnect automatically.")
    log_to_db("WARNING", "Bot disconnected from Discord. Will attempt to reconnect automatically.")


@bot.event
async def on_resumed():
    logger.info("Bot session resumed successfully.")
    log_to_db("INFO", "Bot session resumed successfully.")


@bot.event
async def on_error(event, *args, **kwargs):
    logger.exception("Unhandled error in event '%s'", event)
    log_to_db("ERROR", f"Unhandled error in event '{event}'")


# ─── Scraper loop ─────────────────────────────────────────────────────────────

@tasks.loop(minutes=CHECK_INTERVAL)
async def job_scraper_loop():
    check_memory()

    try:
        cleanup_old_jobs()
        cleanup_old_logs()
    except Exception as e:
        msg = f"[Scraper] DB cleanup failed (non-fatal, continuing): {e}"
        logger.warning(msg)
        log_to_db("WARNING", msg)

    if should_refresh() or should_refresh_auth():
        logger.info("[Scraper] Token/CF refresh due — refreshing...")
        log_to_db("INFO", "[Scraper] Token/CF refresh due — refreshing...")
        try:
            await refresh_client()
            logger.info("[Scraper] Cookies refreshed successfully.")
            log_to_db("INFO", "[Scraper] Cookies refreshed successfully.")
        except Exception as e:
            msg = f"[Scraper] Could not refresh cookies after all retries: {e}. Continuing with existing cookies."
            logger.warning(msg)
            log_to_db("WARNING", msg)

    try:
        search_channels = get_active_search_channels()
    except Exception as e:
        msg = f"[Scraper] Failed to read search channels from DB: {e}. Skipping this cycle."
        logger.error(msg)
        log_to_db("ERROR", msg)
        return

    if not search_channels:
        logger.info("[Scraper] No active keyword→channel mappings found.")
        log_to_db("INFO", "[Scraper] No active keyword→channel mappings found.")
        return

    msg = f"[Scraper] Running for {len(search_channels)} keyword(s)..."
    logger.info(msg)
    log_to_db("INFO", msg)

    for entry in search_channels:
        keyword    = entry["keyword"]
        channel_id = int(entry["channel_id"])
        channel    = bot.get_channel(channel_id)

        if not channel:
            msg = f"[Scraper] Channel {channel_id} not found for keyword '{keyword}' — skipping."
            logger.warning(msg)
            log_to_db("WARNING", msg)
            continue

        async def _fetch(_kw=keyword):
            return await asyncio.to_thread(fetch_jobs_with_details, query=_kw, count=10)

        try:
            jobs = await with_retry(f"Fetch:{keyword}", _fetch)
        except AuthExpiredError:
            msg = f"[Scraper] Auth expired fetching '{keyword}'. Forcing cookie refresh..."
            logger.warning(msg)
            log_to_db("WARNING", msg)
            try:
                await refresh_client()
                log_to_db("INFO", "[Scraper] Cookies refreshed after 401. Retrying fetch...")
                jobs = await with_retry(f"Fetch:{keyword}:post-refresh", _fetch)
            except Exception as e:
                msg = f"[Scraper] Still failing for '{keyword}' after cookie refresh: {e}. Skipping keyword."
                logger.error(msg)
                log_to_db("ERROR", msg)
                continue
        except Exception as e:
            msg = f"[Scraper] Failed to fetch jobs for '{keyword}' after all retries: {e}. Moving to next keyword."
            logger.error(msg)
            log_to_db("ERROR", msg)
            continue

        new_count = 0
        for job in jobs:
            search  = job.get("search", {})
            details = job.get("details", {})

            job_id = ((search.get("jobTile") or {}).get("job") or {}).get("ciphertext")
            if not job_id:
                msg = f"[Scraper] No ciphertext found for a job under keyword '{keyword}', skipping."
                logger.warning(msg)
                log_to_db("WARNING", msg)
                continue

            try:
                already_posted = is_job_posted(job_id)
            except Exception as e:
                msg = f"[Scraper] DB error checking job {job_id}: {e}. Skipping to avoid duplicate."
                logger.error(msg)
                log_to_db("ERROR", msg)
                continue

            if already_posted:
                continue

            _ch = channel
            _s  = search
            _d  = details

            async def _send_embed(_channel=_ch, _search=_s, _details=_d):
                embed = build_embed(_search, _details)
                return await _channel.send(embed=embed)

            try:
                message = await with_retry(f"SendEmbed:{job_id}", _send_embed)
            except Exception as e:
                msg = f"[Scraper] Failed to send embed for job {job_id} after all retries: {e}. Skipping job."
                logger.error(msg)
                log_to_db("ERROR", msg)
                continue

            async def _post_thread(_msg=message, _search=_s, _details=_d):
                await post_job_thread(_msg, _search, _details)

            try:
                await with_retry(f"PostThread:{job_id}", _post_thread)
            except Exception as e:
                msg = f"[Scraper] Failed to post thread for job {job_id} after all retries: {e}. Embed was still sent."
                logger.error(msg)
                log_to_db("ERROR", msg)

            try:
                title     = search.get("title", "")
                posted_at = (details.get("opening") or {}).get("publishTime", "")
                mark_job_posted(job_id, title, str(posted_at))
            except Exception as e:
                msg = f"[Scraper] DB error marking job {job_id} as posted: {e}. Job may repost next cycle."
                logger.error(msg)
                log_to_db("ERROR", msg)

            new_count += 1
            await asyncio.sleep(1)

        msg = f"  └─ '{keyword}' → #{channel.name}: {new_count} new job(s)"
        logger.info(msg)
        log_to_db("INFO", msg)


@job_scraper_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()


@job_scraper_loop.error
async def scraper_loop_error(error):
    msg = f"[Scraper] Unhandled exception in job_scraper_loop — loop will restart on next tick: {error}"
    logger.exception(msg)
    log_to_db("ERROR", msg)


# ─── Commands ─────────────────────────────────────────────────────────────────

@bot.command(name="add")
@commands.has_permissions(manage_channels=True)
async def add_job(ctx, *, keyword: str):
    add_search_channel(keyword.lower(), str(ctx.channel.id))
    msg = f"[Command] Added keyword '{keyword}' for channel {ctx.channel.id}"
    logger.info(msg)
    log_to_db("INFO", msg)
    await ctx.send(f"✅ Now tracking **{keyword}** in {ctx.channel.mention}")


@bot.command(name="remove")
@commands.has_permissions(manage_channels=True)
async def remove_job(ctx, *, keyword: str):
    remove_search_channel(keyword.lower(), str(ctx.channel.id))
    msg = f"[Command] Removed keyword '{keyword}' from channel {ctx.channel.id}"
    logger.info(msg)
    log_to_db("INFO", msg)
    await ctx.send(f"🗑️ Stopped tracking **{keyword}** in {ctx.channel.mention}")


@bot.command(name="list")
async def list_jobs(ctx):
    entries = get_active_search_channels()
    if not entries:
        await ctx.send("No active job searches configured.")
        return
    lines = [f"• **{e['keyword']}** → <#{e['channel_id']}>" for e in entries]
    await ctx.send("**Active job searches:**\n" + "\n".join(lines))


# ─── Status helpers ───────────────────────────────────────────────────────────

def calculate_uptime() -> str:
    if not BOT_START_TIME:
        return "Unknown"
    delta = datetime.now(tz=timezone.utc) - BOT_START_TIME
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def get_memory_usage() -> str:
    try:
        process = psutil.Process(os.getpid())
        mb = process.memory_info().rss / 1024 / 1024
        return f"{mb:.1f} MB"
    except Exception:
        return "Unknown"


def get_last_refresh() -> str:
    if not _last_refresh_time:
        return "Not refreshed yet"
    delta = datetime.now(tz=timezone.utc) - _last_refresh_time
    m, s  = divmod(int(delta.total_seconds()), 60)
    return f"{m}m {s}s ago"


@bot.command(name="status")
async def status(ctx):
    try:
        jobs_last_hour   = count_recent_jobs(since_minutes=60)
        errors_last_hour = count_recent_errors(since_minutes=60)
    except Exception as e:
        await ctx.send(f"⚠️ Could not retrieve DB stats: {e}")
        return

    try:
        active_channels = get_active_search_channels()
    except Exception as e:
        await ctx.send(f"⚠️ Could not retrieve active channels: {e}")
        return

    embed = discord.Embed(
        title     = "🤖 Bot Status Dashboard",
        color     = discord.Color.green() if errors_last_hour == 0 else discord.Color.orange(),
        timestamp = datetime.now(tz=timezone.utc),
    )
    embed.add_field(name="⏱️ Uptime",             value=calculate_uptime(),       inline=True)
    embed.add_field(name="💾 Memory",             value=get_memory_usage(),        inline=True)
    embed.add_field(name="🔑 Last Token Refresh", value=get_last_refresh(),        inline=True)
    embed.add_field(name="📋 Jobs Posted (1h)",   value=str(jobs_last_hour),       inline=True)
    embed.add_field(name="🔍 Active Keywords",    value=str(len(active_channels)), inline=True)
    embed.add_field(name="❗ Errors (1h)",         value=str(errors_last_hour),     inline=True)

    if active_channels:
        keyword_list = "\n".join(f"• **{e['keyword']}** → <#{e['channel_id']}>" for e in active_channels)
        embed.add_field(name="📡 Tracked Keywords", value=keyword_list, inline=False)

    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
    await ctx.send(embed=embed)


# ─── Shutdown ─────────────────────────────────────────────────────────────────

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
    except Exception as e:
        logger.error("Error during bot shutdown: %s", e)


@bot.event
async def on_close():
    await close_session()


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)