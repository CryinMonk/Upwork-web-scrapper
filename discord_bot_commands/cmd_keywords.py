"""
cmd_keywords.py

Commands for tracking and removing individual keywords.

  !add <keyword>            — smart-add: routes to correct channel via taxonomy
  !remove <keyword> [--all] — remove keyword(s); deletes channel when none remain
"""

import asyncio
import logging

import discord
from discord.ext import commands

from database.database import (
    add_search_channel, remove_search_channel,
    get_active_search_channels,
    is_keyword_tracked, get_keywords_for_channel, deactivate_channel_keywords,
    get_keyword_canonical, set_keyword_metadata,
    get_channel_for_canonical, get_all_active_canonicals,
)
from taxonomy.skill_taxonomy import resolve_skill, suggest_close_canonicals
from discord_bot_commands.cmd_helpers import log, logger


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _resolve_keyword(ctx, keyword_clean: str) -> dict | None:
    """
    Resolve a keyword to its canonical skill group.
    Resolution order: DB cache → exact taxonomy → fuzzy → standalone confirmation.
    Returns the resolved dict, or None if resolution failed or the user cancelled.
    """
    cached = get_keyword_canonical(keyword_clean)
    if cached:
        return {
            "canonical":    cached["canonical"],
            "family":       cached["family"],
            "channel_name": cached["canonical"],
            "confidence":   "exact",
        }

    thinking = await ctx.send(f"🔍 Resolving **{keyword_clean}**…")
    try:
        resolved = resolve_skill(keyword_clean)
    except Exception as e:
        await thinking.delete()
        await ctx.send(f"❌ Skill resolver failed: {e}")
        return None
    await thinking.delete()

    if resolved["confidence"] == "fuzzy":
        await ctx.send(
            f"💡 Interpreted **{keyword_clean}** as `{resolved['canonical']}` — continuing…"
        )

    if resolved["confidence"] == "standalone":
        if not await _confirm_standalone(ctx, keyword_clean):
            return None

    return resolved


async def _confirm_standalone(ctx, keyword_clean: str) -> bool:
    """
    Prompt the user before creating a brand-new channel for an unknown keyword.
    Suggests close matches from actively-tracked canonicals when available.
    Returns True if confirmed, False if cancelled or timed out.
    """
    suggestions = suggest_close_canonicals(keyword_clean, get_all_active_canonicals())

    if suggestions:
        suggestion_list = ", ".join(f"`{s}`" for s in suggestions)
        prompt = (
            f"⚠️ **`{keyword_clean}`** didn't match any known skill alias.\n"
            f"Did you mean one of these? {suggestion_list}\n"
            f"Reply **`yes`** to create a new `#{keyword_clean}` channel anyway, or **`no`** to cancel."
        )
    else:
        prompt = (
            f"⚠️ **`{keyword_clean}`** is not in the skill taxonomy.\n"
            f"Reply **`yes`** to create a new `#{keyword_clean}` channel, or **`no`** to cancel."
        )

    await ctx.send(prompt)

    def check(m):
        return (
            m.author == ctx.author
            and m.channel == ctx.channel
            and m.content.lower() in ("yes", "no")
        )

    try:
        reply = await ctx.bot.wait_for("message", timeout=30.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send(f"⏱️ Timed out — `!add {keyword_clean}` cancelled.")
        return False

    if reply.content.lower() == "no":
        await ctx.send("❌ Cancelled. Use `!add <correct-skill>` to try again.")
        return False

    return True


async def _route_to_existing_channel(ctx, keyword_clean: str, canonical: str, family: str) -> bool:
    """
    Attach keyword to an already-tracked channel for this canonical.
    Returns True if routed (caller should return), False to continue.
    """
    existing_ch_id = get_channel_for_canonical(canonical)
    if not existing_ch_id:
        return False

    target = ctx.bot.get_channel(int(existing_ch_id))
    if not target:
        msg = f"[add] Channel {existing_ch_id} for '{canonical}' missing — recreating."
        logger.warning(msg); log("WARNING", msg)
        deactivate_channel_keywords(existing_ch_id)
        return False

    add_search_channel(keyword_clean, existing_ch_id)
    set_keyword_metadata(keyword_clean, canonical, family)
    log("INFO", f"[add] '{keyword_clean}' (canonical={canonical}) → #{target.name}")

    suffix = (
        f"\n-# `{keyword_clean}` is an alias for `{canonical}` · "
        f"use `!keywords {target.name}` to see all terms"
        if keyword_clean != canonical else ""
    )
    await ctx.send(f"✅ Added **{keyword_clean}** → {target.mention}{suffix}")
    return True


async def _adopt_existing_discord_channel(ctx, keyword_clean: str, canonical: str,
                                          family: str, channel_name: str) -> bool:
    """
    Adopt an existing Discord channel that matches the channel name but isn't tracked yet.
    Returns True if adopted, False to continue.
    """
    existing = discord.utils.get(ctx.guild.text_channels, name=channel_name)
    if not existing:
        return False

    add_search_channel(keyword_clean, str(existing.id))
    set_keyword_metadata(keyword_clean, canonical, family)
    log("INFO", f"[add] '{keyword_clean}' → adopted #{existing.name}")
    await ctx.send(
        f"✅ Tracking **{keyword_clean}** in existing {existing.mention}\n"
        f"-# Canonical group: `{canonical}`"
    )
    return True


async def _create_channel_and_track(ctx, keyword_clean: str, canonical: str,
                                    family: str, channel_name: str) -> None:
    """Create a new Discord channel and start tracking the keyword in it."""
    try:
        new_channel = await ctx.guild.create_text_channel(
            name=channel_name,
            category=ctx.channel.category,
            topic=f"Upwork job listings · skill group: {canonical}",
            reason=f"Job tracker !add by {ctx.author}",
        )
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to create channels.")
        return
    except discord.HTTPException as e:
        await ctx.send(f"❌ Failed to create channel: {e}")
        return

    add_search_channel(keyword_clean, str(new_channel.id))
    set_keyword_metadata(keyword_clean, canonical, family)
    log("INFO", f"[add] '{keyword_clean}' (canonical={canonical}) → created #{new_channel.name}")

    suffix = (
        f"\n-# `{keyword_clean}` is an alias for `{canonical}` · "
        f"add related terms with `!add <skill>`"
        if keyword_clean != canonical else ""
    )
    await ctx.send(
        f"✅ Created {new_channel.mention} and tracking **{keyword_clean}** there.{suffix}"
    )


# ─── Registration ─────────────────────────────────────────────────────────────

def register_keyword_commands(bot: commands.Bot) -> None:

    @bot.command(name="add")
    @commands.has_permissions(manage_channels=True)
    async def add_job(ctx, *, keyword: str):
        """
        Add a keyword to track. Routes to the correct channel automatically.

          !add ts           → #typescript   (alias resolved)
          !add spring-boot  → #java         (grouped under java)
          !add my-new-skill → #my-new-skill (standalone, confirmed by user)
        """
        keyword_clean = keyword.lower().strip()

        if is_keyword_tracked(keyword_clean):
            cached  = get_keyword_canonical(keyword_clean)
            ch_id   = get_channel_for_canonical(cached["canonical"]) if cached else None
            target  = bot.get_channel(int(ch_id)) if ch_id else None
            mention = target.mention if target else f"`#{keyword_clean}`"
            await ctx.send(f"⚠️ **{keyword_clean}** is already tracked in {mention}.")
            return

        resolved = await _resolve_keyword(ctx, keyword_clean)
        if resolved is None:
            return

        canonical    = resolved["canonical"]
        family       = resolved["family"]
        channel_name = resolved["channel_name"]

        if await _route_to_existing_channel(ctx, keyword_clean, canonical, family):
            return
        if await _adopt_existing_discord_channel(ctx, keyword_clean, canonical, family, channel_name):
            return
        await _create_channel_and_track(ctx, keyword_clean, canonical, family, channel_name)

    @bot.command(name="remove")
    @commands.has_permissions(manage_channels=True)
    async def remove_job(ctx, *, keyword: str):
        """
        Remove a keyword from tracking.
        Append --all to remove every keyword in the same canonical group.

          !remove spring       removes just 'spring'; #java stays if others remain
          !remove java --all   removes all java-group keywords and deletes #java
        """
        remove_all    = keyword.lower().strip().endswith(" --all")
        keyword_clean = keyword.lower().strip().removesuffix(" --all")

        if not is_keyword_tracked(keyword_clean):
            await ctx.send(f"⚠️ **{keyword_clean}** is not currently being tracked.")
            return

        cached            = get_keyword_canonical(keyword_clean)
        canonical         = cached["canonical"] if cached else keyword_clean
        target_channel_id = get_channel_for_canonical(canonical)

        if not target_channel_id:
            entries           = get_active_search_channels()
            match             = next((e for e in entries if e["keyword"] == keyword_clean), None)
            target_channel_id = match["channel_id"] if match else None

        if not target_channel_id:
            remove_search_channel(keyword_clean, "")
            await ctx.send(f"🗑️ Stopped tracking **{keyword_clean}** (no channel record found).")
            return

        if remove_all:
            to_remove = get_keywords_for_channel(target_channel_id)
            for kw in to_remove:
                remove_search_channel(kw, target_channel_id)
            removed_display = ", ".join(f"`{k}`" for k in to_remove)
            log("INFO", f"[remove] All keywords for '{canonical}' removed: {to_remove}")
        else:
            remove_search_channel(keyword_clean, target_channel_id)
            removed_display = f"`{keyword_clean}`"
            log("INFO", f"[remove] '{keyword_clean}' removed from {target_channel_id}")

        remaining      = get_keywords_for_channel(target_channel_id)
        target_channel = bot.get_channel(int(target_channel_id))
        channel_ref    = target_channel.mention if target_channel else f"`#{canonical}`"

        if remaining:
            await ctx.send(
                f"🗑️ Removed {removed_display}.\n"
                f"-# {channel_ref} still has {len(remaining)} keyword(s): "
                + ", ".join(f"`{k}`" for k in remaining)
            )
            return

        if target_channel:
            try:
                await target_channel.delete(reason=f"All keywords removed by {ctx.author}")
                await ctx.send(
                    f"🗑️ Removed {removed_display} — "
                    f"no remaining keywords so `#{canonical}` was deleted."
                )
            except discord.Forbidden:
                await ctx.send(
                    f"🗑️ Removed {removed_display}, but I can't delete {channel_ref} "
                    f"(missing permissions)."
                )
            except discord.HTTPException as e:
                await ctx.send(
                    f"🗑️ Removed {removed_display}, but couldn't delete the channel: {e}"
                )
        else:
            await ctx.send(
                f"🗑️ Removed {removed_display}. "
                f"(Channel `{target_channel_id}` no longer exists in Discord.)"
            )