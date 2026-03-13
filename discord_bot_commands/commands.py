"""
commands.py

Thin entry point — imports and wires up all command modules.
The error handler lives here because it applies globally across all commands.

Registration (called once from discordbot.py on_ready):
    from commands import register_commands
    register_commands(bot)

Command index:
  Keyword tracking   (cmd_keywords.py)  — !add, !remove
  Channel management (cmd_channels.py)  — !addch, !removech
  Query / display    (cmd_query.py)     — !list, !keywords
  Taxonomy           (cmd_taxonomy.py)  — !addskill, !addalias, !reloadtaxonomy
  Status             (cmd_status.py)    — !status
"""

from discord.ext import commands

from discord_bot_commands.cmd_helpers  import log, logger
from discord_bot_commands.cmd_keywords import register_keyword_commands
from discord_bot_commands.cmd_channels import register_channel_commands
from discord_bot_commands.cmd_query    import register_query_commands
from discord_bot_commands.cmd_taxonomy import register_taxonomy_commands
from discord_bot_commands.cmd_status   import register_status_commands


def register_commands(bot: commands.Bot) -> None:
    """Wire up every command module and attach the global error handler."""

    register_keyword_commands(bot)
    register_channel_commands(bot)
    register_query_commands(bot)
    register_taxonomy_commands(bot)
    register_status_commands(bot)

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
            pass
        else:
            logger.exception("Unhandled command error in '%s': %s", ctx.command, error)
            log("ERROR", f"Command error in '{ctx.command}': {error}")
            await ctx.send(f"❌ An unexpected error occurred: {error}")