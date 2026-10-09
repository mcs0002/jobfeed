"""Talentsoft description enricher (Crédit Agricole CIB, Amundi, Candriam).

``scrapers/talentsoft.py`` is a LISTING-only scraper: it returns title, url and
location and no body. The source was nonetheless declared ``SCRAPER`` in
``coverage.DESCRIPTION_STRATEGY``, so the generic GET path filled it, and the
generic stripper keeps the portal's navigation ("Espace candidat", "Ma
sélection", language switcher) as the "description": ~600 characters of chrome
on every CA-CIB, Amundi and Candriam role. The 2026-09-29 audit of hidden
`other` roles found an Amundi structurer and CA-CIB front-office roles hidden
for exactly this reason: the tagger saw nothing but the title.

The posting is server-rendered in ``div#contenu-ficheoffre`` (class
``ts-offer-page__content-details``; ``ts-offer-details-content`` is only its
``data-class``), one labelled field after another (``fldjobdescription_*``:
missions, profile, contract, location). Take that subtree only.
"""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .descriptions import _INLINE_WS_RE, _MULTI_NL_RE, _PAD_NL_RE

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
MAX_CHARS = 16000
# Offer pages on every Talentsoft tenant we scrape: /job/<slug>_<id>.aspx.
_OFFER_PATH_RE = re.compile(r"^/job/[^/]+_\d+\.aspx$", re.I)
HOSTS = frozenset({"jobs.ca-cib.com", "jobs.amundi.com", "jobs.candriam.com"})


def is_talentsoft(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.netloc.lower() in HOSTS and bool(_OFFER_PATH_RE.match(parts.path))


def _clean(node) -> str:
    text = node.get_text("\n", strip=True)
    text = _INLINE_WS_RE.sub(" ", text)
    text = _PAD_NL_RE.sub("\n", text)
    text = _MULTI_NL_RE.sub("\n\n", text).strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip() + " ..."
    return text


def description(url: str, session, timeout: int = 15) -> str:
    """Plain-text posting for one Talentsoft offer URL, or "" on any failure."""
    if not is_talentsoft(url):
        return ""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    node = soup.select_one(
        "#contenu-ficheoffre, div.ts-offer-page__content-details, "
        "[data-class='ts-offer-details-content']")
    return _clean(node) if node is not None else ""
