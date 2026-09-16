"""Fetch HR-manager.net (Talent Recruiter) full job ads from the advertisement
page.

The listing scraper (`scrapers/hr_manager.py`) stores the position list's
``ShortDescription`` — a summary teaser (~250 chars). The full ad is NOT in any
JSON endpoint: the ``position/{id}/json`` detail call still only carries
``ShortDescription``. The complete posting lives on the advertisement page the
scraper already stores as the job URL:

    https://candidate.hr-manager.net/ApplicationInit.aspx?cid=..&ProjectId=..&..

That ASPX page is server-rendered; the ad body sits in a
``<div id="AdvertisementContent">`` container (the rest of the page is browser-
warning chrome, apply forms and cookie banners). We extract that container
alone. Public, no auth. Confirmed 2026-07-16: 7110 chars vs. ~250 for the stub.
"""
import re

import requests

from .descriptions import _extract_text

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# The stored URL is the HR-manager candidate advertisement/apply page.
_URL_RE = re.compile(
    r"^https?://[^/]*hr-manager\.net/.*(?:ApplicationInit|ShowAdvertisement)",
    re.I)

_CONTENT_RE = re.compile(r'<div[^>]+id="AdvertisementContent"[^>]*>(.*)',
                         re.I | re.S)


def is_hr_manager(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 15) -> str:
    """Plain-text description for one HR-manager ad URL, or "" on any failure."""
    if not is_hr_manager(url):
        return ""
    getter = session or requests
    try:
        r = getter.get(url, headers=_HEADERS, timeout=timeout,
                       allow_redirects=True)
        if r.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    m = _CONTENT_RE.search(r.text)
    if not m:
        return ""
    # _extract_text tolerates the unbalanced tail after the container match
    # (html.parser is forgiving) and drops the trailing form/footer chrome via
    # its _SKIP_TAGS set.
    return _extract_text(m.group(1))
