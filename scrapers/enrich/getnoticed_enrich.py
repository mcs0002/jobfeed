"""Getnoticed description enricher (Hauck Aufhäuser Lampe on karriere.abnamro.de).

``coverage`` had getnoticed as ``NONE`` ("JS shell detail", probed 2026-07-16),
and that probe was right about the URL it probed: ``scrapers/getnoticed.py``
built ``/stellenangebote/<slug>``, which is the search page. It renders the
listing shell ("Stellenangebote ( 0 )"), and the generic backstop stored those
189 characters on all 12 live rows. The board's own links are
``/stellenangebot/<id>/<slug>`` (singular, with the vacancy id), and that page
is server-rendered with the full posting in ``div.component_vacancy-content``
(its JSON-LD ``description`` is empty). The scraper now builds that URL.
"""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .descriptions import _extract_text

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
_VACANCY_PATH_RE = re.compile(r"^/stellenangebot/\d+(?:/|$)", re.I)
HOSTS = frozenset({"karriere.abnamro.de"})


def is_getnoticed(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.netloc.lower() in HOSTS and bool(_VACANCY_PATH_RE.match(parts.path))


def description(url: str, session, timeout: int = 15) -> str:
    """Plain-text posting for one getnoticed vacancy URL, or "" on any failure."""
    if not is_getnoticed(url):
        return ""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    node = soup.select_one("div.component_vacancy-content")
    return _extract_text(str(node)) if node is not None else ""
