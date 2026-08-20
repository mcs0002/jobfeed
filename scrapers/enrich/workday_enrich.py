"""Fetch Workday job descriptions via the cxs JSON API.

Workday job pages are JavaScript single-page apps — a plain GET on the public
URL returns an empty shell with no description text, which is why ~24% of the
DB (every Workday row) had no description. The real content lives at the same
cxs endpoint the scraper uses for listings:

    {base}/wday/cxs/{tenant}/{board}{externalPath}
        -> {"jobPostingInfo": {"jobDescription": "<html>...", ...}}

Two things a URL-only enricher can't supply, but the scan can:

  1. The **board** (e.g. "External_Career_Site_Barclays") is in targets.json,
     not in the public job URL.
  2. The detail GET returns 403 (Workday error S22) on a cold session. The
     session must first hit the tenant's listing endpoint; that primes the
     CALYPSO cookies that authorize the detail GET. So we POST a tiny listing
     query once per (base, board) and reuse the primed session.

`WorkdayEnricher` caches one primed session per tenant and is safe to reuse
across many rows. It is NOT thread-safe per session — parallelise across
tenants (one enricher call per tenant thread), not within a tenant.
"""
from urllib.parse import urlsplit

import requests

from .descriptions import _extract_text

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Workday-Client": "2023.43.4",
}


def is_workday(url: str) -> bool:
    # Both public host flavors: {tenant}.wdN.myworkdayjobs.com and the
    # "external site" variant wdN.myworkdaysite.com (PWP, Baird, Golub) —
    # the cxs detail path works identically on either.
    u = (url or "").lower()
    return "myworkdayjobs.com" in u or "myworkdaysite.com" in u


def detail_url(public_url: str, tenant: str, board: str) -> str | None:
    """Map a public Workday job URL to its cxs JSON detail endpoint, given the
    tenant/board from config. Returns None if the URL isn't Workday-shaped.

    The scraper stores either `{base}/{board}{externalPath}` or, for some
    tenants, `{base}{externalPath}` (no board) — so we strip a leading
    `/{board}` if present and re-prefix the canonical cxs path."""
    if not is_workday(public_url) or not tenant or not board:
        return None
    parts = urlsplit(public_url)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path
    if path.startswith(f"/{board}/"):
        path = path[len(board) + 1:]
    if "/wday/cxs/" in path:  # already an API URL
        return base + path
    return f"{base}/wday/cxs/{tenant}/{board}{path}"


class WorkdayEnricher:
    """Fetches Workday descriptions, priming + caching one session per tenant."""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self._sessions: dict[tuple[str, str], requests.Session] = {}

    def _session(self, base: str, tenant: str, board: str,
                 facets: dict | None) -> requests.Session:
        key = (base, board)
        s = self._sessions.get(key)
        if s is None:
            s = requests.Session()
            # Prime: a 1-row listing POST sets the CALYPSO cookies that
            # authorise subsequent detail GETs. Best-effort — if it fails the
            # detail GETs just 403 and we return "".
            try:
                s.post(
                    f"{base}/wday/cxs/{tenant}/{board}/jobs",
                    json={"appliedFacets": facets or {}, "limit": 1,
                          "offset": 0, "searchText": ""},
                    headers=_HEADERS, timeout=self.timeout,
                )
            except requests.RequestException:
                pass
            self._sessions[key] = s
        return s

    def description(self, public_url: str, tenant: str, board: str,
                    facets: dict | None = None) -> str:
        """Plain-text description for one Workday job, or "" on any failure."""
        du = detail_url(public_url, tenant, board)
        if not du:
            return ""
        base = "{0.scheme}://{0.netloc}".format(urlsplit(public_url))
        s = self._session(base, tenant, board, facets)
        try:
            r = s.get(du, headers=_HEADERS, timeout=self.timeout)
            if r.status_code >= 400:
                return ""
            html = (r.json().get("jobPostingInfo") or {}).get("jobDescription") or ""
        except (requests.RequestException, ValueError):
            return ""
        return _extract_text(html)
