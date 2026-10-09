"""Phenom People description enricher (BCG, Oliver Wyman, AllianzGI, EnBW,
Franklin Templeton).

Both Phenom scrapers (``phenom.py``, ``phenom_widgets.py``) are LISTING-only:
the ``refineSearch`` rows carry a ``descriptionTeaser`` and no body. They were
declared ``SCRAPER`` in ``coverage.DESCRIPTION_STRATEGY`` anyway, so the generic
GET path filled them, and the generic stripper reads the rendered DOM. Every
Phenom job page ships the "job expired" template in that DOM, visible or not
("We're sorry… the job you are trying to apply for has been filled."), next to
the careers-site navigation, and that is what got stored: 214 of 407 live BCG
rows and 51 of 139 Oliver Wyman rows on 2026-09-29. All 265 were still on the
live listing; the "filled" text was chrome, not a status.

The posting is in the page's ``phApp.ddo`` JSON at
``jobDetail.data.job.description`` (HTML). A job that really is gone answers
410 with an empty ``job``; that returns "" here, and the generic fallback
refuses a >= 400 page too, so nothing is stored for it. Delisting stays the
listing's job: a role that leaves ``refineSearch`` is delisted by the scan.
"""
import json
import re
from urllib.parse import urlsplit

from .descriptions import _extract_text

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
# Job pages on every Phenom tenant we scrape: /<locale>/<lang>/job/<id>/<slug>.
_JOB_PATH_RE = re.compile(r"^/[^/]+/[^/]+/job/[^/]+(?:/|$)", re.I)
HOSTS = frozenset({
    "careers.bcg.com", "careers.marsh.com", "careers.allianz.com",
    "careers.enbw.com", "careers.franklintempleton.com",
})
_DDO_RE = re.compile(r"phApp\.ddo\s*=\s*")


def is_phenom(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.netloc.lower() in HOSTS and bool(_JOB_PATH_RE.match(parts.path))


def _ddo(html: str) -> dict:
    m = _DDO_RE.search(html)
    if not m:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def description(url: str, session, timeout: int = 15) -> str:
    """Plain-text posting for one Phenom job URL, or "" on any failure."""
    if not is_phenom(url):
        return ""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return ""
    job = (((_ddo(r.text).get("jobDetail") or {}).get("data") or {})
           .get("job") or {})
    body = job.get("description") or ""
    return _extract_text(body) if isinstance(body, str) else ""
