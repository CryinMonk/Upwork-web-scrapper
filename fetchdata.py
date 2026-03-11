import logging
import json
import re

from curl_cffi import requests
from curl_cffi import CurlError
from database import is_job_posted
from graphql_payloads import  SEARCH_QUERY, DETAILS_QUERY
from database import log

GRAPHQL_URL = "https://www.upwork.com/api/graphql/v1"

_session = requests.Session(impersonate="chrome")
logger   = logging.getLogger("fetchdata")

_SB_COOKIE_RE = re.compile(r"^[0-9a-f]{8}sb$")


class AuthExpiredError(Exception):
    """Raised when Upwork returns 401 or 403 — cookies are stale and must be refreshed."""


def _log(level: str, message: str):
    log(level, "fetchdata", message)


def load_config(filepath: str = "config.json") -> dict:
    with open(filepath, "r") as f:
        return json.load(f)


def get_cookies_and_headers() -> tuple[dict, dict]:
    """Read fresh cookies and headers from config.json on every call — never stale."""
    config = load_config()
    return config["COOKIES"], config["HEADERS"]


def _is_logged_in(cookies: dict) -> bool:
    """True if config has a real logged-in session (has user_uid or master_access_token)."""
    return bool(cookies.get("user_uid") or cookies.get("master_access_token"))


def _find_bearer_token(cookies: dict, for_search: bool = False) -> str | None:
    """
    Find the correct bearer token.

    for_search=True:  prioritise search-scoped tokens (UniversalSearchNuxt_vt)
    for_search=False: any valid token
    """
    if for_search:
        search_priority = (
            "UniversalSearchNuxt_vt",
            "visitor_gql_token",
            "oauth2_global_js_token",
        )
        for name in search_priority:
            val = cookies.get(name)
            if val and "oauth2v2" in val:
                return val

    # Logged-in user tokens
    for preferred in ("d7d66d64sb", "oauth2_global_js_token", "UniversalSearchNuxt_vt"):
        val = cookies.get(preferred)
        if val:
            return val

    for k, v in cookies.items():
        if _SB_COOKIE_RE.match(k):
            return v

    val = cookies.get("visitor_topnav_gql_token")
    if val:
        return val

    return None


def _build_headers(cookies: dict, token: str | None, referer: str) -> dict:
    """Build the full set of HTTP headers for Upwork GraphQL requests."""
    request_headers = {
        "accept":                    "*/*",
        "accept-language":           "en-US,en;q=0.9",
        "authorization":             f"bearer {token}" if token else "",
        "content-type":              "application/json",
        "origin":                    "https://www.upwork.com",
        "priority":                  "u=1, i",
        "referer":                   referer,
        "sec-ch-ua":                 '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile":          "?0",
        "sec-ch-ua-platform":        '"Linux"',
        "sec-fetch-dest":            "empty",
        "sec-fetch-mode":            "cors",
        "sec-fetch-site":            "same-origin",
        "user-agent":                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                     "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "x-upwork-accept-language":  "en-US",
    }

    xsrf = cookies.get("XSRF-TOKEN")
    if xsrf:
        request_headers["x-xsrf-token"] = xsrf

    return request_headers


def _do_graphql_post(cookies, headers, payload, params, label="graphql"):
    """Execute a GraphQL POST and handle common error patterns. Returns response."""
    try:
        response = _session.post(
            GRAPHQL_URL,
            params=params,
            cookies=cookies,
            headers=headers,
            json=payload,
            timeout=30,
        )
    except CurlError as e:
        msg = f"[{label}] Network error: {e}"
        logger.error(msg); _log("ERROR", msg)
        raise

    if response.status_code in (401, 403):
        msg = f"[{label}] Auth expired (HTTP {response.status_code})."
        logger.warning(msg); _log("WARNING", msg)
        raise AuthExpiredError(msg)

    if response.status_code != 200:
        msg = f"[{label}] Unexpected status {response.status_code}: {response.text[:300]}"
        logger.error(msg); _log("ERROR", msg)
        response.raise_for_status()

    return response


def fetch_jobs(query: str, count: int = 10, offset: int = 0) -> list:
    """
    Search Upwork for jobs matching `query`.
    Uses userJobSearch for logged-in sessions, visitorJobSearch for visitors.
    Auto-falls-back to visitor if user search returns a permission error.
    """
    try:
        cookies, headers = get_cookies_and_headers()
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        msg = f"[fetch_jobs] Failed to load config: {e}"
        logger.error(msg); _log("ERROR", msg)
        raise

    logged_in    = _is_logged_in(cookies)
    session_type = "user" if logged_in else "visitor"

    msg = f"[fetch_jobs] Session type: {session_type} for query '{query}'"
    logger.info(msg); _log("INFO", msg)

    # if logged_in:
    #     result = _fetch_jobs_user(cookies, headers, query, count, offset)
    #     if result is not None:
    #         return result
    #     logger.warning("[fetch_jobs] User search failed, falling back to visitor search.")

    return _fetch_jobs_visitor(cookies, headers, query, count, offset)


# def _fetch_jobs_user(cookies, headers, query, count, offset) -> list | None:
#     """
#     Try logged-in user search. Returns list on success, None on permission error.
#     """
#     token = _find_bearer_token(cookies, for_search=True)
#     org_uid = cookies.get("current_organization_uid", "")
#
#     request_headers = _build_headers(
#         cookies, token,
#         referer=f"https://www.upwork.com/nx/search/jobs/?q={query}",
#     )
#     if org_uid:
#         request_headers["x-upwork-api-tenantid"] = org_uid
#
#     payload = {
#         "query": USER_SEARCH_QUERY,
#         "variables": {
#             "requestVariables": {
#                 "userQuery": query,
#                 "sort": "recency+desc",
#                 "highlight": True,
#                 "paging": {"offset": offset, "count": count},
#             },
#         },
#     }
#
#     try:
#         response = _do_graphql_post(
#             cookies, request_headers, payload,
#             {"alias": "userJobSearch"}, label=f"fetch_jobs_user:{query}"
#         )
#     except (AuthExpiredError, CurlError):
#         raise
#     except Exception:
#         return None
#
#     try:
#         data = response.json()
#     except (ValueError, json.JSONDecodeError):
#         return None
#
#     if "errors" in data:
#         err_msg = data["errors"][0].get("message", "") if data["errors"] else ""
#         if "permission" in err_msg.lower():
#             logger.warning(f"[fetch_jobs_user] Permission denied: {err_msg}")
#             return None
#         logger.error(f"[fetch_jobs_user] GraphQL error: {err_msg}")
#         return None
#
#     try:
#         nuxt = data["data"]["search"]["universalSearchNuxt"]
#         search_root = nuxt["userJobSearchV1"]
#         results = search_root["results"]
#         paging  = search_root["paging"]
#         msg = f"[fetch_jobs_user] Query '{query}': {paging['total']} total, fetched {len(results)}"
#         logger.info(msg); _log("INFO", msg)
#         return results
#     except (KeyError, TypeError):
#         return None


def _fetch_jobs_visitor(cookies, headers, query, count, offset) -> list:
    """Visitor search — works with search-scoped visitor tokens."""
    token = _find_bearer_token(cookies, for_search=True)

    if not token:
        msg = "[fetch_jobs_visitor] No visitor token available."
        logger.error(msg); _log("ERROR", msg)
        return []

    token_preview = token[:30] + "..." if token else "None"
    logger.info(f"[fetch_jobs_visitor] Using token: {token_preview}")

    request_headers = _build_headers(
        cookies, token,
        referer=f"https://www.upwork.com/nx/search/jobs/?q={query}",
    )

    payload = {
        "query": SEARCH_QUERY,
        "variables": {
            "requestVariables": {
                "userQuery": query,
                "sort": "recency+desc",
                "highlight": True,
                "paging": {"offset": offset, "count": count},
            },
        },
    }

    try:
        response = _do_graphql_post(
            cookies, request_headers, payload,
            {"alias": "visitorJobSearch"}, label=f"fetch_jobs_visitor:{query}"
        )
    except CurlError as e:
        msg = f"[fetch_jobs_visitor] Network error for query '{query}': {e}"
        logger.error(msg); _log("ERROR", msg)
        raise

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as e:
        msg = f"[fetch_jobs_visitor] Failed to decode JSON for query '{query}': {e}"
        logger.error(msg); _log("ERROR", msg)
        return []

    if "errors" in data:
        err_msg = data["errors"][0].get("message", "") if data["errors"] else ""
        msg = f"[fetch_jobs_visitor] GraphQL error for query '{query}': {err_msg}"
        logger.error(msg); _log("ERROR", msg)
        if "data" not in data:
            return []

    try:
        nuxt = data["data"]["search"]["universalSearchNuxt"]
        search_root = nuxt.get("visitorJobSearchV1")
        if not search_root:
            raise KeyError("visitorJobSearchV1 not found")
        results = search_root["results"]
        paging  = search_root["paging"]
        msg = f"[fetch_jobs_visitor] Query '{query}': {paging['total']} total, fetched {len(results)}"
        logger.info(msg); _log("INFO", msg)
        return results
    except (KeyError, TypeError) as e:
        msg = f"[fetch_jobs_visitor] Unexpected response shape for query '{query}': {e}"
        logger.error(msg); _log("ERROR", msg)
        try:
            preview = json.dumps(data)[:800]
        except Exception:
            preview = str(data)[:800]
        logger.error(f"[fetch_jobs_visitor] Response body: {preview}")
        _log("ERROR", f"[fetch_jobs_visitor] Response body: {preview}")
        return []


def fetch_job_details(ciphertext: str) -> dict:
    """
    Fetch full details for a single job by ciphertext.
    Uses the SAME search-scoped token and header format as the search endpoint —
    the details API requires the same authorization.
    """
    try:
        cookies, headers = get_cookies_and_headers()
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        msg = f"[fetch_job_details] Failed to load config for {ciphertext}: {e}"
        logger.error(msg); _log("ERROR", msg)
        raise

    # Use the search-scoped token — that works for job search
    token = _find_bearer_token(cookies, for_search=True)
    if not token:
        # Fallback to whatever is in the Authorization header
        token = headers.get("authorization", "").removeprefix("Bearer ").removeprefix("bearer ").strip()

    # Build headers the same way as the search request — must match
    request_headers = _build_headers(
        cookies, token,
        referer=f"https://www.upwork.com/nx/search/jobs/",
    )

    # Only include tenant ID for logged-in sessions
    if _is_logged_in(cookies):
        org_uid = cookies.get("current_organization_uid")
        if org_uid:
            request_headers["x-upwork-api-tenantid"] = org_uid

    payload = {
        "query": DETAILS_QUERY,
        "variables": {"id": ciphertext},
    }

    try:
        response = _do_graphql_post(
            cookies, request_headers, payload,
            {"alias": "gql-query-get-visitor-job-details"},
            label=f"fetch_job_details:{ciphertext}"
        )
    except (AuthExpiredError, CurlError):
        raise

    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as e:
        msg = f"[fetch_job_details] Failed to decode JSON for {ciphertext}: {e}"
        logger.error(msg); _log("ERROR", msg)
        return {}

    # Check for GraphQL-level errors
    if "errors" in data:
        err_msg = data["errors"][0].get("message", "") if data["errors"] else ""
        msg = f"[fetch_job_details] GraphQL error for {ciphertext}: {err_msg}"
        logger.warning(msg); _log("WARNING", msg)
        # If data is also present, try to extract partial result
        if "data" not in data:
            return {}

    try:
        return data["data"]["jobPubDetails"] or {}
    except (KeyError, TypeError) as e:
        msg = f"[fetch_job_details] Unexpected response shape for {ciphertext}: {e}"
        logger.error(msg); _log("ERROR", msg)
        try:
            preview = json.dumps(data)[:500]
        except Exception:
            preview = str(data)[:500]
        logger.error(f"[fetch_job_details] Response body: {preview}")
        _log("ERROR", f"[fetch_job_details] Response body: {preview}")
        return {}


def fetch_jobs_with_details(query: str, count: int = 10) -> list[dict]:
    """Fetch jobs and enrich only NEW jobs with full details."""

    jobs     = fetch_jobs(query=query, count=count)
    enriched = []

    for job in jobs:
        ciphertext = (job.get("jobTile") or {}).get("job", {}).get("ciphertext")

        if not ciphertext:
            msg = f"[fetch_jobs_with_details] No ciphertext for job id={job.get('id')}, skipping."
            logger.warning(msg); _log("WARNING", msg)
            enriched.append({"search": job, "details": {}})
            continue

        if is_job_posted(ciphertext):
            enriched.append({"search": job, "details": {}})
            continue

        title = job.get("title", ciphertext)
        msg   = f"[fetch_jobs_with_details] Fetching details for new job: {title} ({ciphertext})"
        logger.info(msg); _log("INFO", msg)

        try:
            details = fetch_job_details(ciphertext)
        except AuthExpiredError:
            # Details 403 is non-fatal — include job without details rather
            # than triggering a full auth refresh
            msg = f"[fetch_jobs_with_details] 403 on details for {ciphertext} — including without details."
            logger.warning(msg); _log("WARNING", msg)
            details = {}
        except Exception as e:
            msg = f"[fetch_jobs_with_details] Could not fetch details for {ciphertext}: {e}. Including without details."
            logger.warning(msg); _log("WARNING", msg)
            details = {}

        enriched.append({"search": job, "details": details})

    return enriched