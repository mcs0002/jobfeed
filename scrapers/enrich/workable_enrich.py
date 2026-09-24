"""Fetch Workable job descriptions via the public v2 detail API.

Workable's hosted job page (apply.workable.com/<account>/j/<shortcode>/) is a
client-rendered SPA, so a plain GET enriches nothing. The listing API the
scraper uses carries no body either. The full posting lives at:

    https://apply.workable.com/api/v2/accounts/<account>/jobs/<shortcode>
    -> {"description": "<html>", "requirements": "<html>", "benefits": "<html>"}

Public, no auth. account + shortcode are both in the stored job URL.
"""
import re

import requests

from .descriptions import _extract_text

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
            "Accept": "application/json"}

# https://apply.workable.com/<account>/j/<shortcode>/
_URL_RE = re.compile(r"^https?://apply\.workable\.com/([^/]+)/j/([^/]+)")

_BODY_FIELDS = ("description", "requirements", "benefits")


def is_workable(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 20) -> str:
    """Plain-text description for one Workable job URL, or "" on any failure."""
    m = _URL_RE.match(url or "")
    if not m:
        return ""
    account, shortcode = m.groups()
    getter = session or requests
    try:
        r = getter.get(
            f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}",
            headers=_HEADERS, timeout=timeout,
        )
        if r.status_code >= 400:
            return ""
        data = r.json()
    except (requests.RequestException, ValueError):
        return ""
    html = "\n".join(data.get(f) or "" for f in _BODY_FIELDS if (data.get(f) or "").strip())
    return _extract_text(html)
