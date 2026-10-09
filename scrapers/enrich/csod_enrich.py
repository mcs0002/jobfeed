"""Fetch Cornerstone OnDemand (CSOD) job descriptions via the jobDetails API.

CSOD career sites (e.g. worldbankgroup.csod.com) are SPAs: the public
requisition page is a JS shell and the search service only returns title /
location / dates, no body. The description lives behind a SEPARATE service path
from the search (this is why every career-site/v1 guess 404s):

    GET {base}/services/x/job-requisition/v2/requisitions/{reqId}/jobDetails?cultureId=1
        -> {"data": {"externalDescription": "<html>", ...}}

Two things the call needs:
  1. A bearer JWT embedded in the career-site home page (`"token":"..."`), bound
     to the cookies that same GET sets. So we prime once per tenant and reuse
     the session — exactly like the Workday enricher.
  2. The requisitionId and tenant, both parsed from the stored job URL
     ({tenant}.csod.com/ux/ats/careersite/{site}/home/requisition/{reqId}).

`CsodEnricher` caches one primed session per tenant. NOT thread-safe per
session — parallelise across tenants, not within one.
"""
import re

import requests

from .descriptions import _extract_text

_URL_RE = re.compile(
    r"^https?://([^.]+)\.csod\.com/ux/ats/careersite/(\d+)/home/requisition/([^/?#]+)"
)
_TOKEN_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def is_csod(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


class CsodEnricher:
    """Fetches CSOD descriptions, priming + caching one session per tenant."""

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self._sessions: dict[str, requests.Session | None] = {}

    def _session(self, base: str, tenant: str, site_id: str) -> requests.Session | None:
        if tenant in self._sessions:
            return self._sessions[tenant]
        s = requests.Session()
        s.headers.update(_HEADERS)
        try:
            home = s.get(
                f"{base}/ux/ats/careersite/{site_id}/home?c={tenant}",
                timeout=self.timeout,
            )
            m = _TOKEN_RE.search(home.text)
            if not m:
                self._sessions[tenant] = None
                return None
            s.headers["Authorization"] = f"Bearer {m.group(1)}"
        except requests.RequestException:
            self._sessions[tenant] = None
            return None
        self._sessions[tenant] = s
        return s

    def description(self, url: str) -> str:
        """Plain-text description for one CSOD requisition URL, or "" on failure."""
        m = _URL_RE.match(url or "")
        if not m:
            return ""
        tenant, site_id, req_id = m.groups()
        base = f"https://{tenant}.csod.com"
        s = self._session(base, tenant, site_id)
        if s is None:
            return ""
        try:
            r = s.get(
                f"{base}/services/x/job-requisition/v2/requisitions/{req_id}/jobDetails",
                params={"cultureId": 1}, timeout=self.timeout,
            )
            if r.status_code >= 400:
                return ""
            data = (r.json() or {}).get("data") or {}
        except (requests.RequestException, ValueError):
            return ""
        return _extract_text(data.get("externalDescription") or "")
