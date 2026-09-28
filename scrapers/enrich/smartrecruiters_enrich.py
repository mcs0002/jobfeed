"""Fetch SmartRecruiters job descriptions from the public posting API.

The listing scraper (`scrapers/smartrecruiters.py`) stores each posting's `ref`
as the job URL — and that `ref` IS the per-posting API endpoint:

    https://api.smartrecruiters.com/v1/companies/{company}/postings/{postingId}

That endpoint returns JSON, not HTML, so the generic `enrich_one` GET + strip
path scraped the raw API JSON into `description` (the `{"id":...,"name":...}`
junk seen on ~650 `sr_` rows). The real body lives in `jobAd.sections`, each a
`{"title": ..., "text": "<html>"}` object keyed by `companyDescription`,
`jobDescription`, `qualifications`, `additionalInformation`. We GET the same
URL, pull those sections in reading order, and run their HTML through the shared
`_extract_text` for clean plain text.

Public, no auth. Everything needed is already in the stored URL, so like the
Oracle enricher this needs no config threaded in.
"""
import re

import requests

from .descriptions import _extract_text

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}

_URL_RE = re.compile(
    r"^https?://api\.smartrecruiters\.com/v1/companies/[^/]+/postings/[^/?]+",
    re.IGNORECASE,
)

# jobAd.sections keys carrying the posting body, in reading order. The company
# blurb comes first, then the role, qualifications, and any extra info.
_SECTION_ORDER = ("companyDescription", "jobDescription", "qualifications",
                  "additionalInformation")


def is_smartrecruiters(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 12) -> str:
    """Plain-text description for one SmartRecruiters posting API URL, or "" on
    any failure (expired posting, network error, parse error)."""
    if not is_smartrecruiters(url):
        return ""
    getter = session or requests
    try:
        r = getter.get(url, headers=_HEADERS, timeout=timeout)
        if r.status_code >= 400:
            return ""
        sections = r.json().get("jobAd", {}).get("sections", {})
    except (requests.RequestException, ValueError, AttributeError):
        return ""
    if not isinstance(sections, dict):
        return ""
    parts = []
    for key in _SECTION_ORDER:
        sec = sections.get(key) or {}
        text = (sec.get("text") or "").strip()
        if text:
            parts.append(text)
    # Include any unexpected extra sections too, so a tenant's custom block
    # isn't silently dropped.
    for key, sec in sections.items():
        if key in _SECTION_ORDER:
            continue
        if isinstance(sec, dict):
            text = (sec.get("text") or "").strip()
            if text:
                parts.append(text)
    if not parts:
        return ""
    return _extract_text("\n".join(parts))


if __name__ == "__main__":
    import sys
    for u in sys.argv[1:]:
        print(f"=== {u} ===")
        print(description(u)[:400])
        print()
