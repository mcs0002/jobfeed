"""Fetch Goldman Sachs job descriptions via the first-party "Higher" GraphQL API.

The public GS careers portal (higher.gs.com) is a Next.js SPA — a plain GET on
a /roles/<id> URL returns an empty shell. The listing GraphQL query the scraper
uses (roleSearch) carries no body either. The full posting lives behind the
single-role query:

    POST https://api-higher.gs.com/gateway/api/v1/graphql
      query($id:String!){ role(externalSourceId:$id, externalSourceFetch:false)
                          { jobTitle descriptionHtml } }

`externalSourceId` is the NUMERIC role id. The stored URL/role id is suffixed
with the experience group (e.g. "176980_GS_MID_CAREER"); the API only accepts
the leading number ("176980"). Public, no auth.
"""
import re

import requests

from .descriptions import _extract_text

_URL_RE = re.compile(r"^https?://higher\.gs\.com/roles/([^/?#]+)")
_API = "https://api-higher.gs.com/gateway/api/v1/graphql"
_QUERY = ("query($id:String!){ role(externalSourceId:$id, externalSourceFetch:false)"
          "{ descriptionHtml } }")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Content-Type": "application/json",
    "Origin": "https://higher.gs.com",
    "Referer": "https://higher.gs.com/results",
}


def is_goldman(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 20) -> str:
    """Plain-text description for one GS role URL, or "" on any failure."""
    m = _URL_RE.match(url or "")
    if not m:
        return ""
    # "176980_GS_MID_CAREER" -> "176980"; the API rejects the suffixed form.
    source_id = m.group(1).split("_", 1)[0]
    getter = session or requests
    try:
        r = getter.post(_API, json={"query": _QUERY, "variables": {"id": source_id}},
                        headers=_HEADERS, timeout=timeout)
        if r.status_code >= 400:
            return ""
        body = r.json()
    except (requests.RequestException, ValueError):
        return ""
    if body.get("errors"):
        return ""
    role = (body.get("data") or {}).get("role") or {}
    return _extract_text(role.get("descriptionHtml") or "")
