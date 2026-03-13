"""
cmd_taxonomy.py

Commands for managing the skill taxonomy (skill_taxonomy.json).

  !addskill        — guided prompt to add a new skill group
  !addalias        — guided prompt to add aliases to an existing group
  !reloadtaxonomy  — hot-reload skill_taxonomy.json without restarting
"""

import asyncio

from discord.ext import commands

from taxonomy.skill_taxonomy import (
    add_group, add_aliases, resolve_skill,
    reload as reload_taxonomy,
    SKILL_GROUPS, _ALIAS_MAP,
)
from discord_bot_commands.cmd_helpers import log


# ─── Registration ─────────────────────────────────────────────────────────────

def register_taxonomy_commands(bot: commands.Bot) -> None:

    @bot.command(name="addskill")
    @commands.has_permissions(manage_channels=True)
    async def add_skill(ctx):
        """
        Guided 4-step prompt to add a brand-new skill group to skill_taxonomy.json.
        Steps: canonical → channel slug → family → aliases.
        """
        def _check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        async def _ask(prompt: str) -> str | None:
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
            f"-# Step 2 of 4 · Type `{canonical}` to use the canonical as-is, "
            f"or enter a different slug:"
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
        aliases  = list(dict.fromkeys([canonical] + provided))

        try:
            add_group(canonical=canonical, channel=channel, family=family, aliases=aliases)
        except ValueError as e:
            await ctx.send(f"❌ {e}")
            return

        log("INFO", f"[addskill] New group '{canonical}' added by {ctx.author}")
        await ctx.send(
            f"✅ Added skill group `{canonical}` to taxonomy.\n"
            f"-# channel: `{channel}` · family: `{family}` · "
            f"{len(aliases)} alias(es): {', '.join(f'`{a}`' for a in aliases)}"
        )

    @bot.command(name="addalias")
    @commands.has_permissions(manage_channels=True)
    async def add_alias(ctx):
        """
        Guided prompt to add aliases to an existing taxonomy group.
        Accepts the canonical name, any existing alias, or a fuzzy/typo variant.
        """
        def _check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        await ctx.send(
            "🏷️ **Add aliases to existing skill group** — reply `cancel` at any step to abort.\n"
            "**Which canonical group?** (e.g. `java`, `typescript`)\n"
            "-# You can also type an alias or a close match — it will be resolved automatically."
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

        # Resolve: direct canonical → alias map → fuzzy
        canonical = None
        if raw in SKILL_GROUPS:
            canonical = raw
        elif raw in _ALIAS_MAP:
            canonical = _ALIAS_MAP[raw]
            await ctx.send(f"💡 Resolved `{raw}` → canonical `{canonical}`.")
        else:
            resolved = resolve_skill(raw)
            if resolved["confidence"] in ("exact", "fuzzy"):
                canonical = resolved["canonical"]
                await ctx.send(f"💡 Matched `{raw}` → canonical `{canonical}`.")

        if not canonical or canonical not in SKILL_GROUPS:
            await ctx.send(
                f"❌ Could not find a taxonomy group for `{raw}`.\n"
                f"-# Use `!addskill` to create a new group first."
            )
            return

        group    = SKILL_GROUPS[canonical]
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

        log("INFO", f"[addalias] Added to '{canonical}': {added} by {ctx.author}")
        await ctx.send(
            f"✅ Added **{len(added)}** alias(es) to `{canonical}`: "
            + ", ".join(f"`{a}`" for a in added)
        )

    @bot.command(name="reloadtaxonomy")
    @commands.has_permissions(manage_channels=True)
    async def reload_taxonomy_cmd(ctx):
        """Hot-reload skill_taxonomy.json without restarting the bot."""
        try:
            stats = reload_taxonomy()
        except Exception as e:
            await ctx.send(f"❌ Failed to reload taxonomy: {e}")
            return

        log("INFO", f"[reloadtaxonomy] Reloaded by {ctx.author}: {stats}")
        await ctx.send(
            f"✅ Taxonomy reloaded — "
            f"**{stats['groups']}** groups, **{stats['aliases']}** aliases."
        )