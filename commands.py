"""
commands.py

Bot commands registered directly on the bot instance as plain functions.
Each command handler delegates to a focused helper — no class, no Cog.

Registration:
    from commands import register_commands
    register_commands(bot)

Commands:
  !add <keyword>               — smart-add: routes keyword to correct channel via taxonomy
  !remove <keyword> [--all]    — remove keyword(s); deletes channel when none remain
  !addch <canonical>           — create a channel and track all aliases for a taxonomy group
  !removech <channel-name>     — delete a channel and deactivate all its keywords
  !list                        — show all tracked keywords grouped by channel
  !keywords [channel-name]     — show all keywords for a specific channel
  !addskill                    — guided prompt to add a new skill group to the taxonomy
  !addalias                    — guided prompt to add aliases to an existing taxonomy group
  !reloadtaxonomy
  !status
"""

import asyncio
import os
import sqlite3
import logging
import psutil
from datetime import datetime, timezone

import discord
from discord.ext import commands

from database import (
    add_search_channel, remove_search_channel,
    get_active_search_channels,
    is_keyword_tracked, get_keywords_for_channel, deactivate_channel_keywords,
    get_keyword_canonical, set_keyword_metadata,
    get_channel_for_canonical, get_all_active_canonicals,
    count_recent_jobs, count_recent_errors,
    log,
)
from skill_taxonomy import (
    resolve_skill, add_group, add_aliases,
    reload as reload_taxonomy, suggest_close_canonicals,
    SKILL_GROUPS, _ALIAS_MAP,
)

logger = logging.getLogger("commands")


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _log(level: str, message: str) -> None:
    log(level, "commands", message)


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


async def _resolve_keyword(ctx, keyword_clean: str) -> dict | None:
    """
    Resolve a keyword to its canonical skill group.
    Returns the resolved dict or None if resolution failed or was cancelled.
    Handles DB cache, taxonomy lookup, fuzzy notice, and standalone confirmation.
    """
    # DB cache — skip taxonomy entirely if already seen
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
        confirmed = await _confirm_standalone(ctx, keyword_clean)
        if not confirmed:
            return None

    return resolved


async def _confirm_standalone(ctx, keyword_clean: str) -> bool:
    """
    Ask the user to confirm before creating a brand-new channel for an unknown keyword.
    Suggests close matches from actively-tracked canonicals if any exist.
    Returns True if the user confirmed, False if they cancelled or timed out.
    """
    active_canonicals = get_all_active_canonicals()
    suggestions       = suggest_close_canonicals(keyword_clean, active_canonicals)

    if suggestions:
        suggestion_list = ", ".join(f"`{s}`" for s in suggestions)
        prompt_text = (
            f"⚠️ **`{keyword_clean}`** didn't match any known skill alias.\n"
            f"Did you mean one of these? {suggestion_list}\n"
            f"Reply **`yes`** to create a new `#{keyword_clean}` channel anyway, or **`no`** to cancel."
        )
    else:
        prompt_text = (
            f"⚠️ **`{keyword_clean}`** is not in the skill taxonomy.\n"
            f"Reply **`yes`** to create a new `#{keyword_clean}` channel, or **`no`** to cancel."
        )

    await ctx.send(prompt_text)

    def check(m):
        return (
            m.author  == ctx.author
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
    If a channel already exists for this canonical, attach the keyword to it.
    Returns True if routing succeeded (caller should return), False to continue.
    """
    existing_ch_id = get_channel_for_canonical(canonical)
    if not existing_ch_id:
        return False

    target = ctx.bot.get_channel(int(existing_ch_id))
    if not target:
        # Channel deleted outside the bot — clean up so we fall through to recreate
        msg = f"[add] Channel {existing_ch_id} for '{canonical}' missing — recreating."
        logger.warning(msg); _log("WARNING", msg)
        deactivate_channel_keywords(existing_ch_id)
        return False

    add_search_channel(keyword_clean, existing_ch_id)
    set_keyword_metadata(keyword_clean, canonical, family)
    _log("INFO", f"[add] '{keyword_clean}' (canonical={canonical}) → #{target.name}")

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
    If a Discord channel with the right name already exists (but isn't tracked),
    adopt it rather than creating a new one.
    Returns True if adopted, False to continue.
    """
    existing = discord.utils.get(ctx.guild.text_channels, name=channel_name)
    if not existing:
        return False

    add_search_channel(keyword_clean, str(existing.id))
    set_keyword_metadata(keyword_clean, canonical, family)
    _log("INFO", f"[add] '{keyword_clean}' → adopted #{existing.name}")
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
    _log("INFO", f"[add] '{keyword_clean}' (canonical={canonical}) → created #{new_channel.name}")

    suffix = (
        f"\n-# `{keyword_clean}` is an alias for `{canonical}` · "
        f"add related terms with `!add <skill>`"
        if keyword_clean != canonical else ""
    )
    await ctx.send(
        f"✅ Created {new_channel.mention} and tracking **{keyword_clean}** there.{suffix}"
    )


def _build_status_embed(bot, jobs_last_hour: int, errors_last_hour: int,
                        active_channels: list) -> discord.Embed:
    """Build the !status embed from pre-fetched stats."""
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


# ─── Command registration ─────────────────────────────────────────────────────

def register_commands(bot: commands.Bot) -> None:
    """Register all commands directly on the bot instance."""

    # ── !add ──────────────────────────────────────────────────────────────────

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

    # ── !remove ───────────────────────────────────────────────────────────────

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

        # Fallback: scan active channels when metadata is missing
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
            _log("INFO", f"[remove] All keywords for '{canonical}' removed: {to_remove}")
        else:
            remove_search_channel(keyword_clean, target_channel_id)
            removed_display = f"`{keyword_clean}`"
            _log("INFO", f"[remove] '{keyword_clean}' removed from {target_channel_id}")

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

        # No keywords left — delete the Discord channel
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
                await ctx.send(f"🗑️ Removed {removed_display}, but couldn't delete the channel: {e}")
        else:
            await ctx.send(
                f"🗑️ Removed {removed_display}. "
                f"(Channel `{target_channel_id}` no longer exists in Discord.)"
            )

    # ── !addch ────────────────────────────────────────────────────────────────

    @bot.command(name="addch")
    @commands.has_permissions(manage_channels=True)
    async def add_channel(ctx, *, canonical: str):
        """
        Create a channel and track all aliases for a skill group.

        If the canonical exists in skill_taxonomy.json, all its aliases are
        registered automatically with no further input needed.

        If it is not in the taxonomy, the bot asks for a family and then for
        aliases interactively before creating the channel.

          !addch java            → found in JSON → creates #java, tracks all aliases
          !addch solidity        → not in JSON  → prompts for family, then aliases
        """
        canonical_clean = canonical.lower().strip().replace(" ", "-")

        # 1. Direct canonical lookup
        group = SKILL_GROUPS.get(canonical_clean)

        # 2. Exact alias map — handles !addch spring-boot, !addch cpp, !addch c++
        if not group:
            resolved_canonical = _ALIAS_MAP.get(canonical_clean)
            if resolved_canonical:
                canonical_clean = resolved_canonical
                group           = SKILL_GROUPS.get(canonical_clean)

        # 3. Fuzzy match — handles typos and spacing variants like !addch springboot
        if not group:
            resolved = resolve_skill(canonical_clean)
            if resolved["confidence"] in ("exact", "fuzzy"):
                fuzzy_canonical = resolved["canonical"]
                fuzzy_group     = SKILL_GROUPS.get(fuzzy_canonical)
                if fuzzy_group:
                    if resolved["confidence"] == "fuzzy":
                        await ctx.send(
                            f"💡 Interpreted **{canonical_clean}** as `{fuzzy_canonical}` — continuing…"
                        )
                    canonical_clean = fuzzy_canonical
                    group           = fuzzy_group

        def _author_check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        # ── Path A: found in taxonomy — use its data directly ─────────────────
        if group:
            channel_slug = group["channel"]
            family       = group["family"]
            all_aliases  = [a.lower() for a in group["aliases"]]

        # ── Path B: not in taxonomy — ask family then aliases interactively ───
        else:
            await ctx.send(
                f"ℹ️ `{canonical_clean}` is not in the skill taxonomy.\n"
                f"What **family** does it belong to? (e.g. `software`, `design`, `marketing`)\n"
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
                f"The canonical `{canonical_clean}` will be included automatically.\n"
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

            # Always include the canonical itself; deduplicate
            provided    = [a.lower().strip() for a in aliases_msg.content.split() if a.strip()]
            all_aliases = list(dict.fromkeys([canonical_clean] + provided))
            channel_slug = canonical_clean

            # Persist to taxonomy so future !adds and reloads know about this group
            try:
                add_group(
                    canonical=canonical_clean,
                    channel=channel_slug,
                    family=family,
                    aliases=all_aliases,
                )
            except ValueError:
                # Group was added between our check and now — reload and continue
                pass

        # ── Create or adopt the Discord channel ───────────────────────────────
        target = discord.utils.get(ctx.guild.text_channels, name=channel_slug)
        if not target:
            try:
                target = await ctx.guild.create_text_channel(
                    name=channel_slug,
                    category=ctx.channel.category,
                    topic=f"Upwork job listings · skill group: {canonical_clean}",
                    reason=f"Job tracker !addch by {ctx.author}",
                )
            except discord.Forbidden:
                await ctx.send("❌ I don't have permission to create channels.")
                return
            except discord.HTTPException as e:
                await ctx.send(f"❌ Failed to create channel: {e}")
                return

        # ── Register every alias ──────────────────────────────────────────────
        added, skipped = [], []
        for alias in all_aliases:
            if is_keyword_tracked(alias):
                skipped.append(alias)
                continue
            add_search_channel(alias, str(target.id))
            set_keyword_metadata(alias, canonical_clean, family)
            added.append(alias)

        _log("INFO", f"[addch] '#{channel_slug}' — added: {added}, skipped: {skipped}")

        parts = [f"✅ {target.mention} is ready · canonical: `{canonical_clean}` · family: `{family}`"]
        if added:
            parts.append(f"Tracking **{len(added)}** alias(es): {', '.join(f'`{a}`' for a in added)}")
        if skipped:
            parts.append(
                f"⚠️ Already tracked elsewhere ({len(skipped)} skipped): "
                + ", ".join(f"`{a}`" for a in skipped)
            )
        await ctx.send("\n".join(parts))

    # ── !removech ─────────────────────────────────────────────────────────────

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

        _log("INFO", f"[removech] '#{channel_slug}' deleted, {deactivated} keyword(s) deactivated by {ctx.author}")
        await ctx.send(
            f"🗑️ Deleted `#{channel_slug}` and deactivated **{deactivated}** keyword(s)."
        )

    # ── !list ─────────────────────────────────────────────────────────────────

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

    # ── !keywords ─────────────────────────────────────────────────────────────

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

    # ── !addskill ─────────────────────────────────────────────────────────────

    @bot.command(name="addskill")
    @commands.has_permissions(manage_channels=True)
    async def add_skill(ctx):
        """
        Guided prompt to add a brand-new skill group to skill_taxonomy.json.
        Walks through canonical → channel slug → family → aliases step by step.
        """
        def _check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        async def _ask(prompt: str) -> str | None:
            """Send a prompt and return the reply text, or None on timeout/cancel."""
            await ctx.send(prompt)
            try:
                msg = await bot.wait_for("message", timeout=60.0, check=_check)
            except asyncio.TimeoutError:
                await ctx.send("⏱️ Timed out — `!addskill` cancelled.")
                return None
            if msg.content.strip().lower() == "cancel":
                await ctx.send("❌ Cancelled.")
                return None
            return msg.content.strip()

        await ctx.send(
            "🛠️ **Add new skill group** — reply `cancel` at any step to abort.\n"
            "-# Step 1 of 4"
        )

        # Step 1 — canonical
        canonical_raw = await _ask("**Canonical name** (the group key, e.g. `blender-rigging`):")
        if canonical_raw is None:
            return
        canonical = canonical_raw.lower().replace(" ", "-")

        if canonical in SKILL_GROUPS:
            await ctx.send(
                f"❌ `{canonical}` already exists in the taxonomy. "
                f"Use `!addalias` to add aliases to it."
            )
            return

        # Step 2 — channel slug
        channel_raw = await _ask(
            f"**Channel slug** — the Discord channel name this group maps to.\n"
            f"-# Step 2 of 4 · Press enter with `{canonical}` to use the canonical as-is, "
            f"or type a different slug:"
        )
        if channel_raw is None:
            return
        channel = channel_raw.lower().replace(" ", "-") or canonical

        # Step 3 — family
        family_raw = await _ask(
            "**Family** — broad bucket this skill belongs to "
            "(e.g. `software`, `design`, `marketing`, `data`):\n"
            "-# Step 3 of 4"
        )
        if family_raw is None:
            return
        family = family_raw.lower().strip()

        # Step 4 — aliases
        aliases_raw = await _ask(
            f"**Aliases** — space-separated list of all search terms for this group.\n"
            f"-# Step 4 of 4 · `{canonical}` will be included automatically\n"
            f"-# Example: `{canonical} {canonical}-dev {canonical}-expert`"
        )
        if aliases_raw is None:
            return
        provided = [a.lower().strip() for a in aliases_raw.split() if a.strip()]
        aliases  = list(dict.fromkeys([canonical] + provided))  # canonical always first, deduped

        try:
            add_group(canonical=canonical, channel=channel, family=family, aliases=aliases)
        except ValueError as e:
            await ctx.send(f"❌ {e}")
            return

        _log("INFO", f"[addskill] New group '{canonical}' added by {ctx.author}")
        await ctx.send(
            f"✅ Added skill group `{canonical}` to taxonomy.\n"
            f"-# channel: `{channel}` · family: `{family}` · "
            f"{len(aliases)} alias(es): {', '.join(f'`{a}`' for a in aliases)}"
        )

    # ── !addalias ─────────────────────────────────────────────────────────────

    @bot.command(name="addalias")
    @commands.has_permissions(manage_channels=True)
    async def add_alias(ctx):
        """
        Guided prompt to add aliases to an existing taxonomy group.
        Resolves the group via exact → alias map → fuzzy match before asking for aliases.
        """
        def _check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        await ctx.send(
            "🏷️ **Add aliases to existing skill group** — reply `cancel` at any step to abort.\n"
            "**Which canonical group?** (e.g. `java`, `typescript`)\n"
            "-# You can also type an alias or a fuzzy match — it will be resolved automatically."
        )

        try:
            group_msg = await bot.wait_for("message", timeout=60.0, check=_check)
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Timed out — `!addalias` cancelled.")
            return
        if group_msg.content.strip().lower() == "cancel":
            await ctx.send("❌ Cancelled.")
            return

        raw = group_msg.content.strip().lower().replace(" ", "-")

        # Resolve: direct → alias map → fuzzy
        canonical = None
        if raw in SKILL_GROUPS:
            canonical = raw
        elif raw in _ALIAS_MAP:
            canonical = _ALIAS_MAP[raw]
            await ctx.send(f"💡 Resolved `{raw}` → canonical `{canonical}`.")
        else:
            resolved = resolve_skill(raw)
            if resolved["confidence"] == "fuzzy":
                canonical = resolved["canonical"]
                await ctx.send(f"💡 Fuzzy-matched `{raw}` → canonical `{canonical}`.")
            elif resolved["confidence"] == "exact":
                canonical = resolved["canonical"]

        if not canonical or canonical not in SKILL_GROUPS:
            await ctx.send(
                f"❌ Could not find a taxonomy group for `{raw}`.\n"
                f"-# Use `!addskill` to create a new group first."
            )
            return

        group = SKILL_GROUPS[canonical]
        existing = [a.lower() for a in group["aliases"]]
        await ctx.send(
            f"Found **`{canonical}`** (family: `{group['family']}`) "
            f"with {len(existing)} existing alias(es).\n"
            f"**Enter new aliases** — space-separated:\n"
            f"-# Existing: {', '.join(f'`{a}`' for a in existing)}"
        )

        try:
            aliases_msg = await bot.wait_for("message", timeout=120.0, check=_check)
        except asyncio.TimeoutError:
            await ctx.send("⏱️ Timed out — `!addalias` cancelled.")
            return
        if aliases_msg.content.strip().lower() == "cancel":
            await ctx.send("❌ Cancelled.")
            return

        new_aliases = [a.lower().strip() for a in aliases_msg.content.split() if a.strip()]
        if not new_aliases:
            await ctx.send("⚠️ No aliases provided — nothing to add.")
            return

        try:
            added = add_aliases(canonical, new_aliases)
        except ValueError as e:
            await ctx.send(f"❌ {e}")
            return

        if not added:
            await ctx.send("⚠️ No new aliases added — all provided terms are already claimed.")
            return

        _log("INFO", f"[addalias] Added to '{canonical}': {added} by {ctx.author}")
        await ctx.send(
            f"✅ Added **{len(added)}** alias(es) to `{canonical}`: "
            + ", ".join(f"`{a}`" for a in added)
        )

    # ── !reloadtaxonomy ───────────────────────────────────────────────────────

    @bot.command(name="reloadtaxonomy")
    @commands.has_permissions(manage_channels=True)
    async def reload_taxonomy_cmd(ctx):
        """Hot-reload skill_taxonomy.json without restarting the bot."""
        try:
            stats = reload_taxonomy()
        except Exception as e:
            await ctx.send(f"❌ Failed to reload taxonomy: {e}")
            return

        _log("INFO", f"[reloadtaxonomy] Reloaded by {ctx.author}: {stats}")
        await ctx.send(
            f"✅ Taxonomy reloaded — "
            f"**{stats['groups']}** groups, **{stats['aliases']}** aliases."
        )

    # ── !status ───────────────────────────────────────────────────────────────

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

    # ── Error handler ─────────────────────────────────────────────────────────

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need **Manage Channels** permission to use that command.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                f"❌ Missing argument: `{error.param.name}`. "
                f"Use `!help {ctx.command}` for usage."
            )
        elif isinstance(error, commands.CommandNotFound):
            pass  # Silently ignore unknown commands
        else:
            logger.exception("Unhandled command error in '%s': %s", ctx.command, error)
            _log("ERROR", f"Command error in '{ctx.command}': {error}")
            await ctx.send(f"❌ An unexpected error occurred: {error}")