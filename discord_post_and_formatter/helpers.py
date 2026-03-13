import logging
import re
from datetime import datetime, timezone
import discord
from database.database import log

logger = logging.getLogger("helpers")


def _log(level: str, message: str):
    log(level, "helpers", message)


def clean_text(text: str) -> str:
    """Strip Upwork H^highlight^H markers."""
    if not text:
        return text
    return re.sub(r'H\^(.+?)\^H', r'\1', text)


def time_ago(timestamp_str) -> str:
    """ISO string -> 'X minutes ago'."""
    if not timestamp_str:
        return "Unknown"
    try:
        if isinstance(timestamp_str, (int, float)):
            posted = datetime.fromtimestamp(timestamp_str / 1000, tz=timezone.utc)
        else:
            posted = datetime.fromisoformat(str(timestamp_str).replace("Z", "+00:00"))
        seconds = int((datetime.now(tz=timezone.utc) - posted).total_seconds())
        if seconds < 60:    return f"{seconds}s ago"
        if seconds < 3600:  return f"{seconds // 60}m ago"
        if seconds < 86400: return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"
    except Exception as e:
        _log("WARNING", f"[time_ago] {e}")
        return "Unknown"


def format_budget(details: dict) -> str:
    """Budget string derived entirely from details (jobPubDetails payload)."""
    try:
        opening  = details.get("opening") or {}
        info     = opening.get("info") or {}
        job_type = info.get("type", "")
        ext      = opening.get("extendedBudgetInfo") or {}

        if job_type == "HOURLY":
            lo = ext.get("hourlyBudgetMin")
            hi = ext.get("hourlyBudgetMax")
            if lo and hi:  return f"${float(lo):.0f}-${float(hi):.0f}/hr"
            if lo:         return f"${float(lo):.0f}/hr"
            return "Hourly"

        if job_type == "FIXED":
            amount = (opening.get("budget") or {}).get("amount")
            if amount: return f"${float(amount):,.0f}"
            return "Fixed"

        return "N/A"
    except Exception as e:
        _log("WARNING", f"[format_budget] {e}")
        return "N/A"


def format_experience_level(tier) -> str:
    return {
        1: "Entry Level", 2: "Intermediate", 3: "Expert",
        "ENTRY_LEVEL": "Entry Level", "INTERMEDIATE": "Intermediate", "EXPERT": "Expert",
    }.get(tier, str(tier) if tier else "N/A")


def format_client_info(buyer: dict) -> str:
    """
    Payment verification: inferred from totalCharges > 0.
    Upwork requires a verified payment method before any charge can occur.
    """
    try:
        location    = buyer.get("location") or {}
        stats       = buyer.get("stats") or {}
        charges     = stats.get("totalCharges") or {}
        total_spent = float((charges.get("amount") if isinstance(charges, dict) else charges) or 0)

        payment = "\u2705 Verified" if total_spent > 0 else "\u26aa Unverified/Unknown"
        country = location.get("country") or "Unknown"

        if total_spent >= 1_000_000: spent = f"${total_spent/1_000_000:.1f}M"
        elif total_spent >= 1_000:   spent = f"${total_spent/1_000:.1f}K"
        else:                        spent = f"${total_spent:.0f}"

        return f"{payment} | \U0001f4cd {country} | \U0001f4b0 {spent} spent"
    except Exception as e:
        _log("WARNING", f"[format_client_info] {e}")
        return "N/A"


def build_embed(details: dict) -> discord.Embed:
    """
    Summary embed for the channel feed.
    Built entirely from the details (jobPubDetails) payload — no search data needed.
    """
    try:
        opening    = details.get("opening") or {}
        buyer      = details.get("buyer") or {}
        info       = opening.get("info") or {}
        ciphertext = details.get("_ciphertext") or info.get("ciphertext", "")
        job_url    = f"https://www.upwork.com/jobs/{ciphertext}" if ciphertext else "https://www.upwork.com"

        title         = clean_text(info.get("title") or "Untitled")
        posted_str    = time_ago(opening.get("publishTime") or info.get("createdOn"))
        detected_time = datetime.now().strftime("%H:%M")
        budget_str    = format_budget(details)
        level_str     = format_experience_level(opening.get("contractorTier"))
        duration_str  = (opening.get("engagementDuration") or {}).get("label") or "N/A"
        proposals     = (opening.get("clientActivity") or {}).get("totalApplicants", 0)
        client_str    = format_client_info(buyer)

        description = clean_text(opening.get("description") or "")
        preview     = description[:280].strip() + ("..." if len(description) > 280 else "")

        sands  = opening.get("sandsData") or {}
        skills = [s["prefLabel"] for s in (sands.get("ontologySkills") or []) if s.get("prefLabel")]
        if not skills:
            skills = [s["prefLabel"] for s in (sands.get("additionalSkills") or []) if s.get("prefLabel")]
        skills_str = ", ".join(skills[:8]) or "Not listed"

        embed = discord.Embed(
            title       = f"\U0001f4bc {title}",
            url         = job_url,
            color       = discord.Color.green(),
            description = f"```\n{preview}\n```",
        )
        embed.add_field(name="Posted",      value=posted_str,     inline=True)
        embed.add_field(name="Budget/Rate", value=budget_str,     inline=True)
        embed.add_field(name="Level",       value=level_str,      inline=True)
        embed.add_field(name="Duration",    value=duration_str,   inline=True)
        embed.add_field(name="Detected",    value=detected_time,  inline=True)
        embed.add_field(name="Proposals",   value=str(proposals), inline=True)
        embed.add_field(name="Client Info", value=client_str,     inline=False)
        embed.add_field(name="Skills",      value=skills_str,     inline=False)
        embed.add_field(name="\u200b",      value=f"[\U0001f517 Apply Here]({job_url})", inline=False)
        embed.set_footer(text=f"Upwork Job Scraper \u2022 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        return embed

    except Exception as e:
        _log("ERROR", f"[build_embed] {e}")
        ciphertext = details.get("_ciphertext", "")
        return discord.Embed(
            title       = "\U0001f4bc Job",
            url         = f"https://www.upwork.com/jobs/{ciphertext}" if ciphertext else "https://www.upwork.com",
            color       = discord.Color.red(),
            description = "_(embed details unavailable)_",
        )