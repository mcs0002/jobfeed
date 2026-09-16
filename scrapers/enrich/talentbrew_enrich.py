"""Description enricher for TalentBrew / Radancy career boards.

Used by BlackRock, Cargill, Citigroup and ING (all `ats:"talentbrew"`). Their
job-detail URLs have the shape ``https://(careers|jobs).<host>/[lang/]job/<location>/<slug>/<id>``
which collides with the broad SuccessFactors URL matcher (`successfactors_enrich.
is_successfactors`). Routing by URL would send them to the SF detail enricher,
which looks for a `<div class="job">` container that TalentBrew pages don't have
and returns "" — so every TalentBrew firm stored empty descriptions.

We therefore route these by the unambiguous ``talentbrew_`` job-id prefix
(`is_talentbrew`), NOT by URL, and extract the embedded JSON-LD
``JobPosting.description`` (clean, server-rendered SEO markup), falling back to
``og:description``. Plain `requests` works — no SPA execution needed.
"""
import re
from html import unescape

import requests

from .descriptions import _jobposting_description

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

_TAG = re.compile(r"<[^>]+>")
_OG = re.compile(
    r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', re.I)


def is_talentbrew(job_id: str) -> bool:
    return (job_id or "").startswith("talentbrew_")


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html or ""))).strip()


def description(url: str, session: requests.Session | None = None,
                timeout: int = 15) -> str:
    """Plain-text description for one TalentBrew job URL, or "" on any failure."""
    if not url:
        return ""
    getter = session or requests
    try:
        r = getter.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    desc = _jobposting_description(r.text)
    if desc:
        return desc
    m = _OG.search(r.text)
    return _strip(m.group(1)) if m else ""
