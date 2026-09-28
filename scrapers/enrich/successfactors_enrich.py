"""Fetch SAP SuccessFactors / TalentBrew career-site job descriptions.

These sites (careers.ey.com, careers.mizuhoemea.com, jobs.standardchartered.com,
and the many other SuccessFactors RMK career portals) render the job listing as
a JS shell. A plain GET of the job page *does* return server-rendered HTML, but
the generic scrape grabs the cookie-consent banner instead of the body — the
~2000 stored `sf_`/`sfapi_` rows are full of "...Cookie information Welcome to
the EY careers job search site..." junk for exactly this reason.

The real, clean posting is in the page HTML all along: SuccessFactors RMK wraps
it in a single `<div class="job">...</div>` container (with the body span also
carrying `itemprop="description"`). We balance that div and run it through the
shared `_extract_text` chrome-stripper.

No API or auth needed — the body is in the first GET. (There is no public
SuccessFactors JSON endpoint to hit; the `<div class="job">` block is the win.)
A genuinely expired requisition renders "Sorry, this position has been filled."
inside the same div, which extracts to a short string rather than a body — that
is correct: a vanished posting has no description to recover.
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
    "Accept": "text/html,application/xhtml+xml",
}

# SuccessFactors RMK career-site job pages: a `/job/<slug>/<id>/` path on a
# careers.* / jobs.* host. Covers the sample hosts (careers.ey.com,
# careers.mizuhoemea.com, jobs.standardchartered.com — the sfapi_ variant) and
# is general for the family without over-matching arbitrary sites.
_URL_RE = re.compile(
    r"^https?://(?:careers|jobs)\.[^/]+/(?:[^/]+/)?job/[^/]+/[^/]+",
    re.I,
)

# The posting body lives in a single `<div class="job">` container in the
# SuccessFactors RMK template; the description span inside also carries
# itemprop="description". We balance the div to slice out just the body.
_JOB_DIV = re.compile(r'<div[^>]*\bclass="job"[^>]*>', re.I)
_DIV_TAG = re.compile(r"<(/?)div\b", re.I)


def is_successfactors(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def _job_block(html: str) -> str:
    """Inner HTML of the `<div class="job">` posting container, tag-balanced so
    we don't run past it into the page footer. "" if the container is absent."""
    m = _JOB_DIV.search(html)
    if not m:
        return ""
    start = m.end()
    depth = 1
    for t in _DIV_TAG.finditer(html, start):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return html[start:t.start()]
    return html[start:]


def description(url: str, session: requests.Session | None = None,
                timeout: int = 12) -> str:
    """Plain-text job description for one SuccessFactors URL, or "" on any
    failure (expired requisition, network error, missing container)."""
    if not url:
        return ""
    getter = session or requests
    try:
        r = getter.get(url, headers=_HEADERS, timeout=timeout,
                       allow_redirects=True)
        if r.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    return _extract_text(_job_block(r.text))
