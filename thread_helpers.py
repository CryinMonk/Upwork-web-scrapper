import logging
from datetime import datetime
import discord
from helpers import clean_text, format_experience_level, format_budget
from database import log

logger = logging.getLogger("thread_helpers")


def _log(level: str, message: str):
    log(level, "thread_helpers", message)


def _fmt_spent(stats: dict) -> str:
    try:
        charges = stats.get('totalCharges') or {}
        total   = float((charges.get('amount') if isinstance(charges, dict) else charges) or 0)
        if total >= 1_000_000: return f"${total/1_000_000:.1f}M"
        if total >= 1_000:     return f"${total/1_000:.1f}K"
        return f"${total:.0f}"
    except Exception:
        return "N/A"


def _fmt_hire_rate(stats: dict) -> str:
    try:
        posted = stats.get('totalJobsWithHires') or 0
        hired  = stats.get('totalAssignments') or 0
        if not posted: return "N/A"
        return f"{(hired / posted) * 100:.0f}%"
    except Exception:
        return "N/A"


def _fmt_member_since(company: dict) -> str:
    try:
        d = (company or {}).get('contractDate')
        if not d: return "N/A"
        dt = datetime.fromisoformat(str(d).replace("Z", "+00:00"))
        return dt.strftime("%b %Y")
    except Exception:
        return "N/A"


def build_thread_embed(search: dict, details: dict) -> list[discord.Embed]:
    """
    Returns [embed1, embed2]:
      embed1 — full description
      embed2 — client details, job details, skills
    Falls back to a single error embed on failure.
    """
    try:
        opening  = details.get('opening') or {}
        buyer    = details.get('buyer') or {}
        stats    = buyer.get('stats') or {}
        company  = buyer.get('company') or {}
        location = buyer.get('location') or {}
        job      = (search.get('jobTile') or {}).get('job') or {}

        ciphertext = job.get('ciphertext', '')
        job_url    = f"https://www.upwork.com/jobs/{ciphertext}" if ciphertext else "https://www.upwork.com"

        # ── Embed 1: Full description ─────────────────────────────────────────
        full_desc = clean_text(opening.get('description') or search.get('description') or 'No description provided.')
        preview   = full_desc[:4000]
        if len(full_desc) > 4000:
            preview += f"\n\n*[truncated — [read full]({job_url})]*"

        embed1 = discord.Embed(
            title       = "\U0001f4cb Full Job Description",
            description = preview,
            color       = discord.Color.blurple(),
            url         = job_url,
        )

        # ── Embed 2: Details ──────────────────────────────────────────────────
        total_spent  = _fmt_spent(stats)
        hire_rate    = _fmt_hire_rate(stats)
        client_loc   = location.get('country') or 'N/A'
        member_since = _fmt_member_since(company)
        hires_made   = stats.get('totalAssignments', 'N/A')
        jobs_open    = (buyer.get('jobs') or {}).get('openCount', 'N/A')

        # Payment: inferred from spend (Upwork requires verified payment to charge)
        charges     = stats.get('totalCharges') or {}
        spent_float = float((charges.get('amount') if isinstance(charges, dict) else charges) or 0)
        payment     = "\u2705 Verified" if spent_float > 0 else "\u26aa Unknown"

        duration  = (opening.get('engagementDuration') or {}).get('label') or 'N/A'
        exp_level = format_experience_level(opening.get('contractorTier') or job.get('contractorTier'))
        job_type  = ((opening.get('info') or {}).get('type') or job.get('jobType') or 'N/A').replace('_', ' ').title()
        proposals = (opening.get('clientActivity') or {}).get('totalApplicants', 'N/A')
        budget    = format_budget(search, opening)

        sands  = opening.get('sandsData') or {}
        skills = [s['prefLabel'] for s in (sands.get('ontologySkills') or []) if s.get('prefLabel')]
        if not skills:
            skills = [s['prefLabel'] for s in (sands.get('additionalSkills') or []) if s.get('prefLabel')]
        skills_str = ", ".join(skills[:12]) or "Not listed"

        embed2 = discord.Embed(color=discord.Color.blurple())
        embed2.add_field(
            name  = "Client Details",
            value = (
                f"Payment: {payment}\n"
                f"Total Spent: {total_spent}\n"
                f"Hires Made: {hires_made}\n"
                f"Hire Rate: {hire_rate}\n"
                f"Location: {client_loc}\n"
                f"Member Since: {member_since}"
            ),
            inline=False,
        )
        embed2.add_field(
            name  = "Job Details",
            value = (
                f"Budget/Rate: {budget}\n"
                f"Duration: {duration}\n"
                f"Experience Level: {exp_level}\n"
                f"Job Type: {job_type}\n"
                f"Proposals: {proposals}"
            ),
            inline=False,
        )
        embed2.add_field(name="\U0001f6e0\ufe0f Skills", value=skills_str, inline=False)
        embed2.add_field(name="\u200b", value=f"[\U0001f517 Apply on Upwork]({job_url})", inline=False)
        embed2.set_footer(text="Upwork Job Scraper \u2014 Full Details")

        return [embed1, embed2]

    except Exception as e:
        msg = f"[build_thread_embed] Failed for '{search.get('title', 'unknown')}': {e}"
        logger.error(msg)
        _log("ERROR", msg)
        return [discord.Embed(
            title       = "\u26a0\ufe0f Details Unavailable",
            description = "An error occurred while building job details.",
            color       = discord.Color.red(),
        )]