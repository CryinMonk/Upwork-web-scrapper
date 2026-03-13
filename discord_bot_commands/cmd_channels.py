"""
cmd_channels.py

Commands for managing Discord channels as a whole.

  !addch <canonical>        — create a channel and track all taxonomy aliases for a group
  !removech <channel-name>  — delete a channel and deactivate all its keywords
"""

import asyncio

import discord
from discord.ext import commands

from database.database import (
    add_search_channel, is_keyword_tracked,
    set_keyword_metadata, deactivate_channel_keywords,
)
from taxonomy.skill_taxonomy import (
    resolve_skill, add_group,
    SKILL_GROUPS, _ALIAS_MAP,
)
from discord_bot_commands.cmd_helpers import log


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_group(canonical_clean: str) -> tuple[str, dict | None]:
    """
    Resolve an input string to a (canonical, group) pair using three steps:
      1. Direct canonical lookup
      2. Exact alias map
      3. Fuzzy match via resolve_skill
    Returns (resolved_canonical, group_dict) — group_dict is None if unresolved.
    """
    # 1. Direct
    group = SKILL_GROUPS.get(canonical_clean)
    if group:
        return canonical_clean, group

    # 2. Exact alias
    resolved_canonical = _ALIAS_MAP.get(canonical_clean)
    if resolved_canonical:
        return resolved_canonical, SKILL_GROUPS.get(resolved_canonical)

    # 3. Fuzzy
    resolved = resolve_skill(canonical_clean)
    if resolved["confidence"] in ("exact", "fuzzy"):
        fuzzy_canonical = resolved["canonical"]
        fuzzy_group     = SKILL_GROUPS.get(fuzzy_canonical)
        if fuzzy_group:
            return fuzzy_canonical, fuzzy_group

    return canonical_clean, None


async def _create_or_adopt_channel(ctx, channel_slug: str, canonical: str) -> discord.TextChannel | None:
    """
    Return an existing Discord channel with the given slug, or create a new one.
    Returns None and sends an error message on failure.
    """
    target = discord.utils.get(ctx.guild.text_channels, name=channel_slug)
    if target:
        return target

    try:
        return await ctx.guild.create_text_channel(
            name=channel_slug,
            category=ctx.channel.category,
            topic=f"Upwork job listings · skill group: {canonical}",
            reason=f"Job tracker !addch by {ctx.author}",
        )
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to create channels.")
    except discord.HTTPException as e:
        await ctx.send(f"❌ Failed to create channel: {e}")

    return None


def _register_aliases(aliases: list[str], channel_id: str,
                      canonical: str, family: str) -> tuple[list[str], list[str]]:
    """
    Register every alias against the channel in the DB.
    Returns (added, skipped) lists.
    """
    added, skipped = [], []
    for alias in aliases:
        if is_keyword_tracked(alias):
            skipped.append(alias)
            continue
        add_search_channel(alias, channel_id)
        set_keyword_metadata(alias, canonical, family)
        added.append(alias)
    return added, skipped


# ─── Registration ─────────────────────────────────────────────────────────────

def register_channel_commands(bot: commands.Bot) -> None:

    @bot.command(name="addch")
    @commands.has_permissions(manage_channels=True)
    async def add_channel(ctx, *, canonical: str):
        """
        Create a channel and track all aliases for a taxonomy skill group.

        Found in taxonomy → uses all aliases automatically, no further input needed.
        Not found        → prompts for family then aliases, then persists to taxonomy.

          !addch java       → creates #java, tracks all java aliases
          !addch solidity   → not in taxonomy, prompts for family + aliases
        """
        canonical_clean = canonical.lower().strip().replace(" ", "-")

        def _author_check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        canonical_clean, group = _resolve_group(canonical_clean)

        # Notify if fuzzy-resolved to a different canonical
        if group and canonical_clean != canonical.lower().strip().replace(" ", "-"):
            await ctx.send(f"💡 Interpreted **{canonical}** as `{canonical_clean}` — continuing…")

        # ── Path A: found in taxonomy ─────────────────────────────────────────
        if group:
            channel_slug = group["channel"]
            family       = group["family"]
            all_aliases  = [a.lower() for a in group["aliases"]]

        # ── Path B: unknown — prompt for family then aliases ──────────────────
        else:
            await ctx.send(
                f"ℹ️ `{canonical_clean}` is not in the skill taxonomy.\n"
                f"What **family** does it belong to? "
                f"(e.g. `software`, `design`, `marketing`, `data`)\n"
                f"-# Reply `cancel` to abort."
            )
            try:
                family_msg = await bot.wait_for("message", timeout=60.0, check=_author_check)
            except asyncio.TimeoutError:
                await ctx.send("⏱️ Timed out — `!addch` cancelled.")
                return
            if family_msg.content.strip().lower() == "cancel":
                await ctx.send("❌ Cancelled.")
                return

            family = family_msg.content.strip().lower()

            await ctx.send(
                f"Got it — family: `{family}`.\n"
                f"Now enter the **aliases** to track, space-separated.\n"
                f"`{canonical_clean}` will be included automatically.\n"
                f"-# Example: `{canonical_clean} {canonical_clean}-dev {canonical_clean}-expert`\n"
                f"-# Reply `cancel` to abort."
            )
            try:
                aliases_msg = await bot.wait_for("message", timeout=120.0, check=_author_check)
            except asyncio.TimeoutError:
                await ctx.send("⏱️ Timed out — `!addch` cancelled.")
                return
            if aliases_msg.content.strip().lower() == "cancel":
                await ctx.send("❌ Cancelled.")
                return

            provided    = [a.lower().strip() for a in aliases_msg.content.split() if a.strip()]
            all_aliases = list(dict.fromkeys([canonical_clean] + provided))
            channel_slug = canonical_clean

            try:
                add_group(
                    canonical=canonical_clean,
                    channel=channel_slug,
                    family=family,
                    aliases=all_aliases,
                )
            except ValueError:
                pass  # Added concurrently — safe to continue

        # ── Create / adopt channel then register aliases ───────────────────────
        target = await _create_or_adopt_channel(ctx, channel_slug, canonical_clean)
        if target is None:
            return

        added, skipped = _register_aliases(all_aliases, str(target.id), canonical_clean, family)
        log("INFO", f"[addch] '#{channel_slug}' — added: {added}, skipped: {skipped}")

        parts = [f"✅ {target.mention} is ready · canonical: `{canonical_clean}` · family: `{family}`"]
        if added:
            parts.append(f"Tracking **{len(added)}** alias(es): {', '.join(f'`{a}`' for a in added)}")
        if skipped:
            parts.append(
                f"⚠️ Already tracked elsewhere ({len(skipped)} skipped): "
                + ", ".join(f"`{a}`" for a in skipped)
            )
        await ctx.send("\n".join(parts))

    @bot.command(name="removech")
    @commands.has_permissions(manage_channels=True)
    async def remove_channel(ctx, *, channel_name: str):
        """
        Delete a channel and deactivate all keywords pointing to it.

          !removech java
          !removech machine-learning
        """
        channel_slug = channel_name.lower().strip().replace(" ", "-")
        target       = discord.utils.get(ctx.guild.text_channels, name=channel_slug)

        if not target:
            await ctx.send(f"⚠️ No channel named `#{channel_slug}` found.")
            return

        deactivated = deactivate_channel_keywords(str(target.id))

        try:
            await target.delete(reason=f"!removech by {ctx.author}")
        except discord.Forbidden:
            await ctx.send(
                f"⚠️ Deactivated {deactivated} keyword(s), but I can't delete "
                f"{target.mention} (missing permissions)."
            )
            return
        except discord.HTTPException as e:
            await ctx.send(
                f"⚠️ Deactivated {deactivated} keyword(s), but couldn't delete the channel: {e}"
            )
            return

        log("INFO", f"[removech] '#{channel_slug}' deleted, {deactivated} keyword(s) deactivated by {ctx.author}")
        await ctx.send(
            f"🗑️ Deleted `#{channel_slug}` and deactivated **{deactivated}** keyword(s)."
        )