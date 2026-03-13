import re
import logging
import discord
from discord_post_and_formatter.thread_helpers import build_thread_embed
from database.database import log

logger = logging.getLogger("thread_poster")


def _log(level: str, message: str):
    log(level, "thread_poster", message)


def clean_thread_name(title: str) -> str:
    """Strip Upwork highlight markers and enforce Discord's 100-char thread name limit."""
    clean = re.sub(r'H\^(.+?)\^H', r'\1', title)
    return clean[:95] + "..." if len(clean) > 95 else clean


async def post_job_thread(message: discord.Message, details: dict):
    """
    Create a thread on `message` and post full job detail embeds inside it.
    Built entirely from the details payload — no search dict needed.
    """
    info  = (details.get("opening") or {}).get("info") or {}
    title = clean_thread_name(info.get("title") or "Job Details")

    try:
        thread = await message.create_thread(
            name                  = title,
            auto_archive_duration = 1440,
        )
    except discord.Forbidden:
        msg = f"[post_job_thread] Missing permissions to create thread for '{title}'."
        logger.warning(msg); _log("WARNING", msg)
        return None
    except discord.HTTPException as e:
        msg = f"[post_job_thread] HTTP error creating thread for '{title}': status={e.status} — {e.text}"
        logger.error(msg); _log("ERROR", msg)
        return None
    except Exception as e:
        msg = f"[post_job_thread] Unexpected error creating thread for '{title}': {e}"
        logger.error(msg); _log("ERROR", msg)
        return None

    embeds = build_thread_embed(details)
    for embed in embeds:
        try:
            await thread.send(embed=embed)
        except discord.Forbidden:
            msg = f"[post_job_thread] Missing permissions to send embed in thread '{title}'."
            logger.warning(msg); _log("WARNING", msg)
            break
        except discord.HTTPException as e:
            msg = f"[post_job_thread] HTTP error sending embed in thread '{title}': status={e.status} — {e.text}"
            logger.error(msg); _log("ERROR", msg)
            break
        except Exception as e:
            msg = f"[post_job_thread] Unexpected error sending embed in thread '{title}': {e}"
            logger.error(msg); _log("ERROR", msg)
            break

    return thread