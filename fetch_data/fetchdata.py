import logging
import json

from curl_cffi import requests
from curl_cffi import CurlError

from database.database import is_job_posted
from .graphql_payloads import SEARCH_IDS_QUERY, DETAILS_QUERY
from database.database import log
import os as _os

GRAPHQL_URL = "https://www.upwork.com/api/graphql/v1"

CONFIG_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "config.json")
CONFIG_FILE = _os.path.normpath(CONFIG_FILE)


_session = requests.Session(impersonate="chrome")
_session.cookies.clear()

logger = logging.getLogger("fetchdata")

_AUTH_TOKEN_KEYS = ("oauth2_global_js_token", "master_access_token")


class AuthExpiredError(Exception):
    """Raised when Upwork returns 401 or 403 — cookies are stale and must be refreshed."""


def _log(level: str, message: str):
    log(level, "fetchdata", message)


def _load_config() -> tuple[dict, dict]:
    """
    Load cookies and headers exactly as saved by browser_session.py.
    No reconstruction, no overrides — used verbatim.
    Raises AuthExpiredError if auth cookies are missing.
    """
    with open(CONFIG_FILE) as f:
        data = json.load(f)

    cookies: dict = data.get("COOKIES") or {}
    headers: dict = data.get("HEADERS") or {}

    token_key = next((k for k in _AUTH_TOKEN_KEYS if cookies.get(k)), None)
    if not token_key:
        raise AuthExpiredError(
            "No auth token found in config (oauth2_global_js_token / master_access_token). "
            "Re-login required."
        )

    logger.info(
        f"[_load_config] path={CONFIG_FILE!r}  "
        f"auth_key={token_key!r}  token_prefix={cookies[token_key][:20]!r}  "
        f"authorization_header={headers.get('authorization', 'MISSING')[:40]!r}  "
        f"tenant_id={headers.get('x-upwork-api-tenantid', 'MISSING')!r}  "
        f"total_cookies={len(cookies)}  total_headers={len(headers)}"
    )

    return cookies, headers


def _graphql_post(cookies: dict, headers: dict, payload: dict, params: dict, label: str = "graphql"):
    # Clear session cookies so only the freshly-loaded disk cookies are sent
    _session.cookies.clear()
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
        body = response.text[:500] if response.text else "(empty body)"
        msg = f"[{label}] Auth expired (HTTP {response.status_code}) — re-login required. Body: {body}"
        logger.warning(msg); _log("WARNING", msg)
        raise AuthExpiredError(f"[{label}] Auth expired (HTTP {response.status_code}) — re-login required.")

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

        if any(kw in err_msg.lower() for kw in ("unauthorized", "unauthenticated", "forbidden")):
            msg = f"[{label}] GraphQL auth error: {err_msg}"
            logger.warning(msg); _log("WARNING", msg)
            raise AuthExpiredError(msg)

        msg = f"[{label}] GraphQL error: {err_msg}"
        logger.warning(msg); _log("WARNING", msg)
        if "data" not in data:
            return None

    return data


def fetch_new_ciphertexts(query: str, count: int = 10, offset: int = 0) -> list[str]:
    """
    Lightweight authenticated search — fetches ciphertexts only, then filters
    out already-posted jobs. Returns only ciphertexts that need detail fetching.
    """
    cookies, headers = _load_config()

    payload = {
        "query": SEARCH_IDS_QUERY,
        "variables": {
            "requestVariables": {
                "userQuery": query,
                "sort":      "recency",
                "highlight": False,
                "paging":    {"offset": offset, "count": count},
            },
        },
    }

    try:
        response = _graphql_post(
            cookies, headers, payload,
            {"alias": "userJobSearch"},
            label=f"fetch_new_ciphertexts:{query}",
        )
    except CurlError:
        return []

    data = _parse_graphql_response(response, label=f"fetch_new_ciphertexts:{query}")
    if data is None:
        return []

    try:
        search_root = data["data"]["search"]["universalSearchNuxt"]["userJobSearchV1"]
        results     = search_root["results"]
        total       = search_root["paging"]["total"]
    except (KeyError, TypeError) as e:
        msg = f"[fetch_new_ciphertexts] Unexpected response shape for '{query}': {e}"
        logger.error(msg); _log("ERROR", msg)
        return []

    all_ciphertexts = [
        ct for r in results
        if (ct := (r.get("jobTile") or {}).get("job", {}).get("cipherText"))
    ]
    new_ciphertexts = [ct for ct in all_ciphertexts if not is_job_posted(ct)]

    msg = (
        f"[fetch_new_ciphertexts] '{query}': {total} total on Upwork, "
        f"{len(all_ciphertexts)} fetched, {len(new_ciphertexts)} new"
    )
    logger.info(msg); _log("INFO", msg)

    return new_ciphertexts


def fetch_job_details(ciphertext: str) -> dict:
    """Fetch full details for a single job by ciphertext (authenticated)."""
    cookies, headers = _load_config()

    payload = {
        "query": DETAILS_QUERY,
        "variables": {
            "id":                   ciphertext,
            # "isFreelancerOrAgency": True,
            "isLoggedIn":           True,
        },
    }

    response = _graphql_post(
        cookies, headers, payload,
        {"alias": "gql-query-get-auth-job-details"},
        label=f"fetch_job_details:{ciphertext}",
    )

    data = _parse_graphql_response(response, label=f"fetch_job_details:{ciphertext}")
    if data is None:
        return {}

    try:
        return data["data"]["jobAuthDetails"] or {}
    except (KeyError, TypeError) as e:
        msg = f"[fetch_job_details] Unexpected response shape for {ciphertext}: {e}"
        logger.error(msg); _log("ERROR", msg)
        _log("ERROR", f"[fetch_job_details] Response body: {json.dumps(data)[:500]}")
        return {}


def fetch_jobs_with_details(query: str, count: int = 10) -> list[dict]:
    """
    1. Fetch ciphertexts only (lightweight authenticated search request).
    2. Drop already-posted jobs via DB check.
    3. Fetch full details for new jobs only.
    4. Return list of detail dicts — each has _ciphertext attached for the caller.
    """
    new_ciphertexts = fetch_new_ciphertexts(query=query, count=count)

    enriched = []
    for ciphertext in new_ciphertexts:
        logger.info(f"[fetch_jobs_with_details] Fetching details for: {ciphertext}")

        try:
            details = fetch_job_details(ciphertext)
        except (AuthExpiredError, CurlError):
            raise

        if not details:
            msg = f"[fetch_jobs_with_details] No details returned for '{ciphertext}'"
            logger.warning(msg); _log("WARNING", msg)
            continue

        details["_ciphertext"] = ciphertext
        enriched.append(details)

    return enriched