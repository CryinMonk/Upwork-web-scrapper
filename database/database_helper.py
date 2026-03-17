import re

def _extract_job_record(details: dict) -> dict:
    """
    Pull all storable fields out of a jobAuthDetails dict.

    API response structure:
      details["opening"]["job"]       — all job fields (info, description, budget, etc.)
      details["buyer"]["info"]        — stats, location, company
      details["buyer"]["workHistory"] — past contracts

    Description is capped at 200 characters — brief snapshot, not full text.
    """
    # Unwrap the two extra nesting levels the API adds
    opening  = (details.get("opening") or {}).get("job") or {}
    buyer    = details.get("buyer") or {}
    buyer_info = buyer.get("info") or {}

    info     = opening.get("info") or {}
    stats    = buyer_info.get("stats") or {}
    location = buyer_info.get("location") or {}
    ext      = opening.get("extendedBudgetInfo") or {}

    # Title
    title = (info.get("title") or "").strip()

    # Brief description — first 200 chars, no newlines
    raw_desc = opening.get("description") or ""
    # Strip Upwork highlight markers inline (avoid importing helpers to keep DB self-contained)
    raw_desc = re.sub(r'H\^(.+?)\^H', r'\1', raw_desc)
    description = raw_desc.replace("\n", " ").replace("\r", "").strip()[:200]

    # Budget
    job_type_raw = (info.get("type") or "").upper()
    if job_type_raw == "HOURLY":
        lo = ext.get("hourlyBudgetMin")
        hi = ext.get("hourlyBudgetMax")
        if lo and hi: budget = f"${float(lo):.0f}-${float(hi):.0f}/hr"
        elif lo:      budget = f"${float(lo):.0f}/hr"
        else:         budget = "Hourly"
    elif job_type_raw == "FIXED":
        amount = (opening.get("budget") or {}).get("amount")
        budget = f"${float(amount):,.0f}" if amount else "Fixed"
    else:
        budget = "N/A"

    # Experience level
    tier_map = {
        1: "Entry Level", 2: "Intermediate", 3: "Expert",
        "ENTRY_LEVEL": "Entry Level", "INTERMEDIATE": "Intermediate", "EXPERT": "Expert",
    }
    tier = opening.get("contractorTier")
    experience_level = tier_map.get(tier, str(tier) if tier else "N/A")

    # Duration
    duration = (opening.get("engagementDuration") or {}).get("label") or "N/A"

    # Skills — comma-separated, up to 12
    sands  = opening.get("sandsData") or {}
    skills = [s["prefLabel"] for s in (sands.get("ontologySkills") or []) if s.get("prefLabel")]
    if not skills:
        skills = [s["prefLabel"] for s in (sands.get("additionalSkills") or []) if s.get("prefLabel")]
    skills_str = ", ".join(skills[:12]) or "N/A"

    # Client location
    country  = location.get("country") or "N/A"
    city     = location.get("city") or ""
    loc_str  = f"{city}, {country}".strip(", ") if city else country

    # Total spent
    charges = stats.get("totalCharges") or {}
    spent   = float((charges.get("amount") if isinstance(charges, dict) else charges) or 0)
    if spent >= 1_000_000: total_spent = f"${spent/1_000_000:.1f}M"
    elif spent >= 1_000:   total_spent = f"${spent/1_000:.1f}K"
    else:                  total_spent = f"${spent:.0f}"

    # Proposals
    proposals = (opening.get("clientActivity") or {}).get("totalApplicants") or 0

    # posted_at
    posted_at = str(opening.get("publishTime") or info.get("createdOn") or "")

    return {
        "title":            title,
        "posted_at":        posted_at,
        "description":      description,
        "budget":           budget,
        "job_type":         job_type_raw or "N/A",
        "experience_level": experience_level,
        "duration":         duration,
        "skills":           skills_str,
        "location":         loc_str,
        "total_spent":      total_spent,
        "proposals":        proposals,
    }