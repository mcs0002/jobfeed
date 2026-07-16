"""Uniper (careers.uniper.energy) description enricher.

The careers.uniper.energy site is a Next.js/Vercel SPA: the listing API carries
no body, and the human job URL (`/job/<city>-<title>/<idClient>`) resolves
server-side to a `not_found` shell, so a plain GET yields only 404 chrome. Worse,
that URL falsely matches the greedy SuccessFactors matcher, so without this
enricher the row routes to the SF detail lane, finds no `<div class="job">`, and
stays empty.

The backend IS SuccessFactors (company `UniperProd`), fronted by the
server-rendered RMK site `jobs.uniper.energy`. The scraped trailing `idClient`
maps straight to it via one redirect:

    GET career5.successfactors.eu/sfcareer/jobreqcareer?jobId=<id>&company=UniperProd
      -> 302 -> jobs.uniper.energy/job/.../<postingId>/   (has <div class="job">)

So we reuse the SuccessFactors `<div class="job">` extractor — no new parsing.
MUST be registered ahead of successfactors_enrich in DETAIL_ENRICHERS.
"""
import re

import requests

from .descriptions import _extract_text
from .successfactors_enrich import _job_block

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}
# The id is the LAST path segment; the middle (city-title) can itself contain
# slashes (titles like "...(w/m/d)"), so match greedily up to the trailing id.
_URL_RE = re.compile(r"^https?://careers\.uniper\.energy/job/.+/(\d+)/?$", re.I)
_SF = "https://career5.successfactors.eu/sfcareer/jobreqcareer"


def is_uniper(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 15) -> str:
    """Plain-text description for one Uniper careers URL, or "" on failure."""
    m = _URL_RE.match(url or "")
    if not m:
        return ""
    getter = session or requests
    try:
        r = getter.get(_SF, params={"jobId": m.group(1), "company": "UniperProd"},
                       headers=_HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    return _extract_text(_job_block(r.text))
