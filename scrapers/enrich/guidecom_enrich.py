"""GuideCom (connect.guidecom.de) description enricher.

GuideCom is only the APPLICATION backend — the stored
`connect.guidecom.de/jobportal/helaba/viewAusschreibung/<ref>.html` URL is a
bare application form with zero ad text (the generic stripper drops the `<form>`
and returns ~660 chars of footer chrome). The actual posting lives on the
Helaba career site, server-rendered, no auth:

    https://www.helaba.com/de/karriere/stellenangebote/<slug>_<ref>.php

The `<ref>` equals the guidecom job id, but the `<slug>` can't be rebuilt from
the title (umlauts, dropped stopwords), so we map ref -> detail URL from the
Helaba board listing (`data-url` attrs), fetched once per run and cached. The ad
body is isolated in `div#blockMain` (whole-page extraction drags in the cookie
wall).
"""
import re

import requests

from .descriptions import _extract_text

_URL_RE = re.compile(
    r"^https?://connect\.guidecom\.de/jobportal/helaba/viewAusschreibung/"
    r"([^/]+?)\.html", re.I)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}
_LISTING = "https://www.helaba.com/de/karriere/jobs-stellenangebote.php"
_BASE = "https://www.helaba.com"
# ref -> detail URL map, built once per process (one short-lived scan run).
_MAP_CACHE: dict | None = None


def is_guidecom(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def _ref_map(session) -> dict:
    global _MAP_CACHE
    if _MAP_CACHE is not None:
        return _MAP_CACHE
    out: dict = {}
    try:
        r = session.get(_LISTING, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        for path in re.findall(r'data-url="(/de/karriere/stellenangebote/[^"]+?_'
                               r'([^"/]+?)\.php)"', r.text):
            detail_path, ref = path
            out.setdefault(ref, _BASE + detail_path)
    except requests.RequestException:
        pass
    _MAP_CACHE = out
    return out


def description(url: str, session: requests.Session | None = None,
                timeout: int = 15) -> str:
    """Plain-text posting for one guidecom application URL, or "" on failure."""
    m = _URL_RE.match(url or "")
    if not m:
        return ""
    getter = session or requests
    detail = _ref_map(getter).get(m.group(1))
    if not detail:
        return ""
    try:
        r = getter.get(detail, headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException:
        return ""
    from bs4 import BeautifulSoup
    node = BeautifulSoup(r.text, "html.parser").select_one("#blockMain")
    return _extract_text(str(node)) if node else _extract_text(r.text)
