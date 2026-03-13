"""
cmd_status.py

Bot health dashboard command.

  !status  — embed showing uptime, memory, jobs posted, errors, active keywords
"""

import os
import sqlite3
import psutil
from datetime import datetime, timezone

import discord
from discord.ext import commands

from database.database import count_recent_jobs, count_recent_errors, get_active_search_channels
from discord_bot_commands.cmd_helpers import log


# ─── Stat helpers ─────────────────────────────────────────────────────────────

def _uptime_str(bot) -> str:
    start = getattr(bot, "start_time", None)
    if not start:
        return "Unknown"
    delta  = datetime.now(tz=timezone.utc) - start
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


def _memory_str() -> str:
    try:
        mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        return f"{mb:.1f} MB"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "Unknown"


def _last_refresh_str(bot) -> str:
    last = getattr(bot, "last_refresh_time", None)
    if not last:
        return "Not refreshed yet"
    delta = datetime.now(tz=timezone.utc) - last
    m, s  = divmod(int(delta.total_seconds()), 60)
    return f"{m}m {s}s ago"


def _build_status_embed(bot, jobs_last_hour: int, errors_last_hour: int,
                        active_channels: list) -> discord.Embed:
    embed = discord.Embed(
        title     = "🤖 Bot Status Dashboard",
        color     = discord.Color.green() if errors_last_hour == 0 else discord.Color.orange(),
        timestamp = datetime.now(tz=timezone.utc),
    )
    embed.add_field(name="⏱️ Uptime",             value=_uptime_str(bot),         inline=True)
    embed.add_field(name="💾 Memory",             value=_memory_str(),             inline=True)
    embed.add_field(name="🔑 Last Token Refresh", value=_last_refresh_str(bot),   inline=True)
    embed.add_field(name="📋 Jobs Posted (1h)",   value=str(jobs_last_hour),       inline=True)
    embed.add_field(name="🔍 Active Keywords",    value=str(len(active_channels)), inline=True)
    embed.add_field(name="❗ Errors (1h)",         value=str(errors_last_hour),     inline=True)

    if active_channels:
        channel_groups: dict[str, list[str]] = {}
        for e in active_channels:
            channel_groups.setdefault(e["channel_id"], []).append(e["keyword"])
        keyword_list = "\n".join(
            f"• <#{ch_id}> — {', '.join(kws)}"
            for ch_id, kws in channel_groups.items()
        )
        embed.add_field(name="📡 Tracked Keywords", value=keyword_list, inline=False)

    return embed


# ─── Registration ─────────────────────────────────────────────────────────────

def register_status_commands(bot: commands.Bot) -> None:

    @bot.command(name="status")
    async def status(ctx):
        """Bot health dashboard."""
        try:
            jobs_last_hour   = count_recent_jobs(since_minutes=60)
            errors_last_hour = count_recent_errors(since_minutes=60)
            active_channels  = get_active_search_channels()
        except sqlite3.Error as e:
            await ctx.send(f"⚠️ Could not retrieve stats: {e}")
            return

        embed = _build_status_embed(bot, jobs_last_hour, errors_last_hour, active_channels)
        embed.set_footer(text=f"Requested by {ctx.author.display_name}")
        await ctx.send(embed=embed)