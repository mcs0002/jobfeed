"""Fetch Eightfold AI career-portal job descriptions via the apply API.

Eightfold portals (e.g. HSBC's portal.careers.hsbc.com) are JS SPAs: a plain GET
on a /careers/job/<id> URL returns the app shell, and the listing scraper ends up
storing the page's <head> blob — for HSBC's tenant that's a giant
`{"themeOptions": {...}}` CSS theme JSON plus login URLs, not the job body.

The real posting lives behind the per-position apply API on the same host:

    GET https://<host>/api/apply/v2/jobs/<positionId>
    -> {"job_description": "<html>...", "name": ..., ...}

`<positionId>` is the numeric id in the /careers/job/<id> URL. Public, no auth,
no session priming — everything the call needs is in the stored URL, so like the
Oracle enricher this parses it straight from the URL.
"""
import re

import requests

from .descriptions import _extract_text

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# https://<host>/careers/job/<numericPositionId>[/...|?...|#...]
_URL_RE = re.compile(r"^(https?://[^/]+)/careers/job/(\d+)")


def is_eightfold(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 12) -> str:
    """Plain-text description for one Eightfold job URL, or "" on any failure
    (expired position, network error, parse error)."""
    m = _URL_RE.match(url or "")
    if not m:
        return ""
    base_url, position_id = m.groups()
    getter = session or requests
    try:
        r = getter.get(
            f"{base_url}/api/apply/v2/jobs/{position_id}",
            headers=_HEADERS, timeout=timeout,
        )
        if r.status_code >= 400:
            return ""
        data = r.json()
    except (requests.RequestException, ValueError):
        return ""
    return _extract_text(data.get("job_description") or "")
