"""Fetch iCIMS job descriptions via the in_iframe=1 server-rendered view.

The normal iCIMS job page (`https://<tenant>.icims.com/jobs/<id>/<slug>/job`)
is a ~500KB JS shell: the posting body lives in an <iframe> the portal chrome
loads client-side, so the generic enricher extracts ~20 chars of nav and the
JS-shell guard reports the row unenrichable. Observed 2026-07-03: all 18
StoneX rows NULL after a full day of inline + nightly passes.

Appending `?in_iframe=1` to the SAME url returns the iframe's own document —
fully server-rendered, ~38KB, with the posting body inside a
`<div class="iCIMS_JobContent">` container. We extract that container alone,
so the surrounding portal chrome (cookie banner, "Welcome page" nav, sign-in
links) never reaches the stored text.
"""
import re

import requests

from .descriptions import _extract_text, HEADERS

# https://<tenant>.icims.com/jobs/<id>/<slug>/job[?...]
_URL_RE = re.compile(r"^https?://[^/]+\.icims\.com/jobs/\d+/", re.I)

_CONTENT_RE = re.compile(
    r'<div[^>]+class="[^"]*iCIMS_JobContent[^"]*"[^>]*>(.*)', re.I | re.S)


def is_icims(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 12) -> str:
    """Plain-text description for one iCIMS job URL, or "" on any failure."""
    if not is_icims(url):
        return ""
    sep = "&" if "?" in url else "?"
    getter = session or requests
    try:
        r = getter.get(url + sep + "in_iframe=1", headers=HEADERS,
                       timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    m = _CONTENT_RE.search(r.text)
    # _extract_text tolerates the unbalanced tail after the container match
    # (html.parser is forgiving); fall back to the whole iframe doc if the
    # container class ever changes.
    return _extract_text(m.group(1) if m else r.text)
