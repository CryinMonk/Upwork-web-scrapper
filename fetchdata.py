import logging
import json
from json_helper import get_json
from curl_cffi import requests
from curl_cffi import CurlError
from database import is_job_posted
from graphql_payloads import SEARCH_IDS_QUERY, DETAILS_QUERY
from database import log


GRAPHQL_URL = "https://www.upwork.com/api/graphql/v1"

_session = requests.Session(impersonate="chrome")
logger   = logging.getLogger("fetchdata")


class AuthExpiredError(Exception):
    """Raised when Upwork returns 401 or 403 — cookies are stale and must be refreshed."""


def _log(level: str, message: str):
    log(level, "fetchdata", message)


def _prepare_request(referer: str) -> tuple[dict, dict]:
    """Load cookies and build headers for a GraphQL request."""
    cookies = get_json()["COOKIES"]
    token   = next(
        (cookies[n] for n in ("UniversalSearchNuxt_vt", "visitor_gql_token", "oauth2_global_js_token")
         if cookies.get(n)),
        None,
    )
    headers = {
        "accept":                   "*/*",
        "accept-language":          "en-US,en;q=0.9",
        "authorization":            f"bearer {token}" if token else "",
        "content-type":             "application/json",
        "origin":                   "https://www.upwork.com",
        "priority":                 "u=1, i",
        "referer":                  referer,
        "sec-ch-ua":                '"Chromium";v="145", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile":         "?0",
        "sec-ch-ua-platform":       '"Linux"',
        "sec-fetch-dest":           "empty",
        "sec-fetch-mode":           "cors",
        "sec-fetch-site":           "same-origin",
        "user-agent":               "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "x-upwork-accept-language": "en-US",
    }
    if xsrf := cookies.get("XSRF-TOKEN"):
        headers["x-xsrf-token"] = xsrf
    return cookies, headers


def _graphql_post(cookies, headers, payload, params, label="graphql"):
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


def _parse_graphql_response(response, label: str) -> dict | None:
    """Parse JSON and return data dict, or None on error."""
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as e:
        msg = f"[{label}] Failed to decode JSON: {e}"
        logger.error(msg); _log("ERROR", msg)
        return None

    if "errors" in data:
        err_msg = data["errors"][0].get("message", "") if data["errors"] else ""
        msg = f"[{label}] GraphQL error: {err_msg}"
        logger.warning(msg); _log("WARNING", msg)
        if "data" not in data:
            return None

    return data


def fetch_new_ciphertexts(query: str, count: int = 10, offset: int = 0) -> list[str]:
    """
    Lightweight search — fetches ciphertexts only, then filters out already-posted jobs.
    Returns only the ciphertexts that are new and need detail fetching.
    """
    cookies, headers = _prepare_request(
        referer=f"https://www.upwork.com/nx/search/jobs/?q={query}",
    )
    payload = {
        "query": SEARCH_IDS_QUERY,
        "variables": {
            "requestVariables": {
                "userQuery": query,
                "sort": "recency+desc",
                "highlight": False,
                "paging": {"offset": offset, "count": count},
            },
        },
    }

    try:
        response = _graphql_post(
            cookies, headers, payload,
            {"alias": "visitorJobSearch"},
            label=f"fetch_new_ciphertexts:{query}",
        )
    except CurlError:
        return []

    data = _parse_graphql_response(response, label=f"fetch_new_ciphertexts:{query}")
    if data is None:
        return []

    try:
        search_root = data["data"]["search"]["universalSearchNuxt"]["visitorJobSearchV1"]
        results     = search_root["results"]
        total       = search_root["paging"]["total"]
    except (KeyError, TypeError) as e:
        msg = f"[fetch_new_ciphertexts] Unexpected response shape for '{query}': {e}"
        logger.error(msg); _log("ERROR", msg)
        return []

    # Extract ciphertexts and drop already-posted jobs immediately
    all_ciphertexts = [
        ct for r in results
        if (ct := (r.get("jobTile") or {}).get("job", {}).get("ciphertext"))
    ]
    new_ciphertexts = [ct for ct in all_ciphertexts if not is_job_posted(ct)]

    msg = (
        f"[fetch_new_ciphertexts] '{query}': {total} total on Upwork, "
        f"{len(all_ciphertexts)} fetched, {len(new_ciphertexts)} new"
    )
    logger.info(msg); _log("INFO", msg)

    return new_ciphertexts


def fetch_job_details(ciphertext: str) -> dict:
    """Fetch full details for a single job by ciphertext."""
    cookies, headers = _prepare_request(
        referer="https://www.upwork.com/nx/search/jobs/",
    )
    payload = {
        "query": DETAILS_QUERY,
        "variables": {"id": ciphertext},
    }

    response = _graphql_post(
        cookies, headers, payload,
        {"alias": "gql-query-get-visitor-job-details"},
        label=f"fetch_job_details:{ciphertext}",
    )

    data = _parse_graphql_response(response, label=f"fetch_job_details:{ciphertext}")
    if data is None:
        return {}

    try:
        return data["data"]["jobPubDetails"] or {}
    except (KeyError, TypeError) as e:
        msg = f"[fetch_job_details] Unexpected response shape for {ciphertext}: {e}"
        logger.error(msg); _log("ERROR", msg)
        _log("ERROR", f"[fetch_job_details] Response body: {json.dumps(data)[:500]}")
        return {}


def fetch_jobs_with_details(query: str, count: int = 10) -> list[dict]:
    """
    1. Fetch ciphertexts only (lightweight search request)
    2. Drop already-posted jobs via DB check
    3. Fetch full details for new jobs only
    4. Return list of detail dicts — each has _ciphertext attached for the caller
    """
    new_ciphertexts = fetch_new_ciphertexts(query=query, count=count)

    enriched = []
    for ciphertext in new_ciphertexts:
        logger.info(f"[fetch_jobs_with_details] Fetching details for: {ciphertext}")

        try:
            details = fetch_job_details(ciphertext)
        except (AuthExpiredError, CurlError):
            raise  # Re-raise so discordbot can handle auth refresh

        if not details:
            msg = f"[fetch_jobs_with_details] Empty details for {ciphertext}, skipping."
            logger.warning(msg); _log("WARNING", msg)
            continue

        # Attach ciphertext so callers don't need to track it separately
        details["_ciphertext"] = ciphertext
        enriched.append(details)

    return enriched