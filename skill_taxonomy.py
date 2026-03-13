# """
# skill_taxonomy.py
#
# Thin loader for skill_taxonomy.json.
#
# - Reads the JSON on import and builds an in-memory alias index.
# - Call reload() to hot-reload after the JSON has been edited or written to
#   at runtime (e.g. by the !addskill / !addalias commands).
# - resolve_skill() is the only function the rest of the codebase needs.
# """
#
# import json
# import re
# import os
# import logging
# from difflib import get_close_matches
#
# logger = logging.getLogger("skill_taxonomy")
#
# TAXONOMY_FILE = os.path.join(os.path.dirname(__file__), "skill_taxonomy.json")
#
# # Module-level state — replaced wholesale by _load() / reload()
# SKILL_GROUPS: dict[str, dict] = {}
# _ALIAS_MAP:   dict[str, str]  = {}
# _ALL_ALIASES: list[str]       = []
#
#
# # ─── Internal loader ──────────────────────────────────────────────────────────
#
# def _load() -> None:
#     """Read skill_taxonomy.json and rebuild the alias index."""
#     global SKILL_GROUPS, _ALIAS_MAP, _ALL_ALIASES
#
#     with open(TAXONOMY_FILE, encoding="utf-8") as f:
#         data = json.load(f)
#
#     alias_map: dict[str, str] = {}
#     for canonical, meta in data.items():
#         for alias in meta.get("aliases", []):
#             key = alias.lower()
#             if key in alias_map and alias_map[key] != canonical:
#                 logger.warning(
#                     "Duplicate alias '%s': claimed by '%s' and '%s'. Keeping '%s'.",
#                     key, alias_map[key], canonical, alias_map[key],
#                 )
#                 continue
#             alias_map[key] = canonical
#
#     SKILL_GROUPS = data
#     _ALIAS_MAP   = alias_map
#     _ALL_ALIASES = list(alias_map.keys())
#     logger.debug("Taxonomy loaded: %d groups, %d aliases.", len(SKILL_GROUPS), len(_ALIAS_MAP))
#
#
# def _persist() -> None:
#     """Write current SKILL_GROUPS back to skill_taxonomy.json."""
#     with open(TAXONOMY_FILE, "w", encoding="utf-8") as f:
#         json.dump(SKILL_GROUPS, f, indent=2)
#
#
# # ─── Public API ───────────────────────────────────────────────────────────────
#
# def reload() -> dict:
#     """
#     Hot-reload skill_taxonomy.json without restarting the bot.
#     Returns stats so the caller can confirm what was loaded.
#     Triggered by the !reloadtaxonomy command.
#     """
#     _load()
#     return {"groups": len(SKILL_GROUPS), "aliases": len(_ALIAS_MAP)}
#
#
# def resolve_skill(keyword: str) -> dict:
#     """
#     Resolve a keyword to its canonical skill group.
#
#     Resolution order:
#       1. Exact alias match  — O(1) dict lookup
#       2. Fuzzy match        — difflib at 0.82 cutoff, catches typos
#       3. Standalone         — unknown keyword, will get its own channel
#
#     Returns:
#         {
#             canonical:    str   e.g. "java"
#             channel_name: str   e.g. "java"
#             family:       str   e.g. "software"
#             confidence:   str   "exact" | "fuzzy" | "standalone"
#         }
#     """
#     kw = keyword.lower().strip().replace(" ", "-")
#     kw = re.sub(r"[^a-z0-9\.\#\+\-]", "", kw)
#
#     # 1. Exact
#     canonical = _ALIAS_MAP.get(kw)
#     if canonical:
#         meta = SKILL_GROUPS[canonical]
#         return {
#             "canonical":    canonical,
#             "channel_name": meta["channel"],
#             "family":       meta["family"],
#             "confidence":   "exact",
#         }
#
#     # 2. Fuzzy — catches "djangoo", "typescrpit", "kubernets"
#     close = get_close_matches(kw, _ALL_ALIASES, n=1, cutoff=0.70)
#     if close:
#         canonical = _ALIAS_MAP[close[0]]
#         meta      = SKILL_GROUPS[canonical]
#         return {
#             "canonical":    canonical,
#             "channel_name": meta["channel"],
#             "family":       meta["family"],
#             "confidence":   "fuzzy",
#         }
#
#     # 3. Standalone — unknown skill, gets its own channel
#     slug = re.sub(r"[^a-z0-9\-]", "", kw)[:25].strip("-")
#     return {
#         "canonical":    slug,
#         "channel_name": slug,
#         "family":       "other",
#         "confidence":   "standalone",
#     }
#
#
# def add_group(canonical: str, channel: str, family: str, aliases: list[str]) -> None:
#     """
#     Add a brand-new skill group, persist to JSON, and reload the index.
#     Raises ValueError if the canonical already exists.
#     Called by the !addskill command.
#     """
#     if canonical in SKILL_GROUPS:
#         raise ValueError(f"Group '{canonical}' already exists. Use !addalias to extend it.")
#
#     SKILL_GROUPS[canonical] = {"channel": channel, "family": family, "aliases": aliases}
#     _persist()
#     _load()
#
#
# def add_aliases(canonical: str, new_aliases: list[str]) -> list[str]:
#     """
#     Append new aliases to an existing group, skipping any that are already
#     claimed by any group. Persists and reloads the index.
#     Returns the list of aliases that were actually added.
#     Called by the !addalias command.
#     """
#     if canonical not in SKILL_GROUPS:
#         raise ValueError(f"Group '{canonical}' not found in taxonomy.")
#
#     existing_in_group = set(a.lower() for a in SKILL_GROUPS[canonical]["aliases"])
#     added = []
#     for a in new_aliases:
#         key = a.lower()
#         if key in existing_in_group:
#             continue
#         if key in _ALIAS_MAP:
#             logger.warning("Alias '%s' already belongs to '%s', skipping.", key, _ALIAS_MAP[key])
#             continue
#         SKILL_GROUPS[canonical]["aliases"].append(a)
#         added.append(a)
#
#     if added:
#         _persist()
#         _load()
#     return added
#
#
# def taxonomy_stats() -> dict:
#     return {
#         "groups":   len(SKILL_GROUPS),
#         "aliases":  len(_ALIAS_MAP),
#         "families": sorted({m["family"] for m in SKILL_GROUPS.values()}),
#     }
#
#
# # ─── Load on import ───────────────────────────────────────────────────────────
# _load()


"""
skill_taxonomy.py

Thin loader for skill_taxonomy.json.

- Reads the JSON on import and builds an in-memory alias index.
- Call reload() to hot-reload after the JSON has been edited or written to
  at runtime (e.g. by the !addskill / !addalias commands).
- resolve_skill() is the only function the rest of the codebase needs.
"""

import json
import re
import os
import logging
from difflib import get_close_matches

logger = logging.getLogger("skill_taxonomy")

TAXONOMY_FILE = os.path.join(os.path.dirname(__file__), "skill_taxonomy.json")

# Module-level state — replaced wholesale by _load() / reload()
SKILL_GROUPS: dict[str, dict] = {}
_ALIAS_MAP:   dict[str, str]  = {}
_ALL_ALIASES: list[str]       = []


# ─── Internal loader ──────────────────────────────────────────────────────────

def _load() -> None:
    """Read skill_taxonomy.json and rebuild the alias index."""
    global SKILL_GROUPS, _ALIAS_MAP, _ALL_ALIASES

    with open(TAXONOMY_FILE, encoding="utf-8") as f:
        data = json.load(f)

    alias_map: dict[str, str] = {}
    for canonical, meta in data.items():
        for alias in meta.get("aliases", []):
            key = alias.lower()
            if key in alias_map and alias_map[key] != canonical:
                logger.warning(
                    "Duplicate alias '%s': claimed by '%s' and '%s'. Keeping '%s'.",
                    key, alias_map[key], canonical, alias_map[key],
                )
                continue
            alias_map[key] = canonical

    SKILL_GROUPS = data
    _ALIAS_MAP   = alias_map
    _ALL_ALIASES = list(alias_map.keys())
    logger.debug("Taxonomy loaded: %d groups, %d aliases.", len(SKILL_GROUPS), len(_ALIAS_MAP))


def _persist() -> None:
    """Write current SKILL_GROUPS back to skill_taxonomy.json."""
    with open(TAXONOMY_FILE, "w", encoding="utf-8") as f:
        json.dump(SKILL_GROUPS, f, indent=2)


# ─── Public API ───────────────────────────────────────────────────────────────

def reload() -> dict:
    """
    Hot-reload skill_taxonomy.json without restarting the bot.
    Returns stats so the caller can confirm what was loaded.
    Triggered by the !reloadtaxonomy command.
    """
    _load()
    return {"groups": len(SKILL_GROUPS), "aliases": len(_ALIAS_MAP)}


def resolve_skill(keyword: str) -> dict:
    """
    Resolve a keyword to its canonical skill group.

    Resolution order:
      1. Exact alias match  — O(1) dict lookup
      2. Fuzzy match        — difflib at 0.75 cutoff, catches typos
      3. Standalone         — unknown keyword, will get its own channel

    Returns:
        {
            canonical:    str   e.g. "java"
            channel_name: str   e.g. "java"
            family:       str   e.g. "software"
            confidence:   str   "exact" | "fuzzy" | "standalone"
        }
    """
    kw = keyword.lower().strip().replace(" ", "-")
    kw = re.sub(r"[^a-z0-9\.\#\+\-]", "", kw)

    # 1. Exact
    canonical = _ALIAS_MAP.get(kw)
    if canonical:
        meta = SKILL_GROUPS[canonical]
        return {
            "canonical":    canonical,
            "channel_name": meta["channel"],
            "family":       meta["family"],
            "confidence":   "exact",
        }

    # 2. Fuzzy — catches "djangoo", "typescrpit", "kubernets", "jara", "javr"
    close = get_close_matches(kw, _ALL_ALIASES, n=1, cutoff=0.70)
    if close:
        canonical = _ALIAS_MAP[close[0]]
        meta      = SKILL_GROUPS[canonical]
        return {
            "canonical":    canonical,
            "channel_name": meta["channel"],
            "family":       meta["family"],
            "confidence":   "fuzzy",
        }

    # 3. Standalone — unknown skill, gets its own channel
    slug = re.sub(r"[^a-z0-9\-]", "", kw)[:25].strip("-")
    return {
        "canonical":    slug,
        "channel_name": slug,
        "family":       "other",
        "confidence":   "standalone",
    }


def add_group(canonical: str, channel: str, family: str, aliases: list[str]) -> None:
    """
    Add a brand-new skill group, persist to JSON, and reload the index.
    Raises ValueError if the canonical already exists.
    Called by the !addskill command.
    """
    if canonical in SKILL_GROUPS:
        raise ValueError(f"Group '{canonical}' already exists. Use !addalias to extend it.")

    SKILL_GROUPS[canonical] = {"channel": channel, "family": family, "aliases": aliases}
    _persist()
    _load()


def add_aliases(canonical: str, new_aliases: list[str]) -> list[str]:
    """
    Append new aliases to an existing group, skipping any that are already
    claimed by any group. Persists and reloads the index.
    Returns the list of aliases that were actually added.
    Called by the !addalias command.
    """
    if canonical not in SKILL_GROUPS:
        raise ValueError(f"Group '{canonical}' not found in taxonomy.")

    existing_in_group = set(a.lower() for a in SKILL_GROUPS[canonical]["aliases"])
    added = []
    for a in new_aliases:
        key = a.lower()
        if key in existing_in_group:
            continue
        if key in _ALIAS_MAP:
            logger.warning("Alias '%s' already belongs to '%s', skipping.", key, _ALIAS_MAP[key])
            continue
        SKILL_GROUPS[canonical]["aliases"].append(a)
        added.append(a)

    if added:
        _persist()
        _load()
    return added


def suggest_close_canonicals(keyword: str, active_canonicals: list[str], n: int = 3) -> list[str]:
    """
    Given a keyword that resolved as standalone, suggest close canonical names
    from the currently-active tracked list. Used by the !add confirmation prompt.
    Returns up to `n` suggestions (may be empty).
    """
    kw = keyword.lower().strip()
    return get_close_matches(kw, active_canonicals, n=n, cutoff=0.6)


def taxonomy_stats() -> dict:
    return {
        "groups":   len(SKILL_GROUPS),
        "aliases":  len(_ALIAS_MAP),
        "families": sorted({m["family"] for m in SKILL_GROUPS.values()}),
    }


# ─── Load on import ───────────────────────────────────────────────────────────
_load()