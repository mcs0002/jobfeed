"""Fetch Glencore careers job descriptions from the page's JSON-LD JobPosting.

The Glencore listing scraper (`scrapers/glencore.py`) reads only title/location/
date from the first-party Magnolia careers API (`/.rest/api/v2/careers/`); that
listing payload carries no body, and the public job page mixes the real posting
in with site navigation chrome that is NOT wrapped in <nav>/<header> tags — so
the generic `_extract_text` enricher stored the whole nav menu + GDPR footer as
the "description" for all ~1815 glencore_ rows.

The page does, however, embed a schema.org JobPosting as JSON-LD:

    <script type="application/ld+json">{"@type":"JobPosting",
        "description":"<p>We are seeking ...</p>...", ...}</script>

`description` is the role body as HTML (responsibilities/requirements), with no
nav and no cookie/GDPR footer. We parse that one block and run it through
`_extract_text` for plain text. Public, no auth, no session priming; a single
GET on the stored job URL is enough.

The Magnolia detail API isn't addressable by id (`/.rest/api/v2/careers/{id}`
404s — req ids appear as Workday-style `R200001619` and as plain integers, but
neither resolves there), so the embedded JSON-LD is the reliable source.
"""
import re

import requests

from .descriptions import _jobposting_description

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

# www.glencore.com/<locale>/careers/jobs/<reqId> — reqId is R200001619 or 1838.
_URL_RE = re.compile(
    r"^https?://(?:www\.)?glencore\.com/[^/]+/careers/jobs/[^/?#]+", re.I
)


def is_glencore(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 12) -> str:
    """Plain-text job body for one Glencore careers URL, or "" on any failure
    (expired posting, network error, missing/garbled JSON-LD)."""
    if not is_glencore(url):
        return ""
    getter = session or requests
    try:
        r = getter.get(url, headers=_HEADERS, timeout=timeout,
                       allow_redirects=True)
        if r.status_code >= 400:
            return ""
        html_text = r.text
    except requests.RequestException:
        return ""

    # Shared parser (handles Glencore's raw control chars via strict=False).
    return _jobposting_description(html_text)
