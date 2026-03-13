"""
cmd_query.py

Read-only query commands — no DB writes, no channel creation.

  !list                  — show all tracked keywords grouped by channel
  !keywords [channel]    — show all keywords for a specific channel
"""

import discord
from discord.ext import commands

from database.database import get_active_search_channels, get_keywords_for_channel, get_keyword_canonical


# ─── Registration ─────────────────────────────────────────────────────────────

def register_query_commands(bot: commands.Bot) -> None:

    @bot.command(name="list")
    async def list_jobs(ctx):
        """Show all tracked keywords grouped by channel."""
        entries = get_active_search_channels()
        if not entries:
            await ctx.send("No active job searches configured.")
            return

        channel_groups: dict[str, list[str]] = {}
        for e in entries:
            channel_groups.setdefault(e["channel_id"], []).append(e["keyword"])

        lines = []
        for ch_id, kws in channel_groups.items():
            ch     = bot.get_channel(int(ch_id))
            ch_ref = ch.mention if ch else f"`(deleted:{ch_id})`"
            lines.append(f"• {ch_ref} — {', '.join(f'`{k}`' for k in sorted(kws))}")

        embed = discord.Embed(
            title       = f"📡 Active Job Searches ({len(channel_groups)} channel(s))",
            description = "\n".join(lines),
            color       = discord.Color.blurple(),
        )
        embed.set_footer(text="!keywords <n> for details · !add <skill> to add more")
        await ctx.send(embed=embed)

    @bot.command(name="keywords")
    async def show_keywords(ctx, *, channel_name: str = ""):
        """
        Show all search keywords tracked in a channel.

          !keywords             uses the current channel
          !keywords javascript  looks up #javascript
        """
        if channel_name:
            target = discord.utils.get(ctx.guild.text_channels, name=channel_name.lower().strip())
            if not target:
                await ctx.send(f"⚠️ No channel named `{channel_name}` found.")
                return
        else:
            target = ctx.channel

        kws = get_keywords_for_channel(str(target.id))
        if not kws:
            await ctx.send(f"No keywords are tracked in {target.mention}.")
            return

        cached    = get_keyword_canonical(kws[0])
        canonical = cached["canonical"] if cached else "?"
        family    = cached["family"]    if cached else "?"
        kw_lines  = "\n".join(f"  • `{k}`" for k in sorted(kws))

        await ctx.send(
            f"**{target.mention}** tracks {len(kws)} keyword(s):\n"
            f"{kw_lines}\n"
            f"-# canonical: `{canonical}` · family: `{family}`"
        )