"""Crédit Mutuel RHEC description enricher (La Française Group).

La Française is a ``generic`` source on recrutement.creditmutuel.fr, filtered
to its entities with ``socs=``. The offer page is server-rendered, but the
generic stripper picks the recruiting-process banner ("Votre candidature en 4
étapes ...") over the posting, so all 14 live rows stored ~450 characters of it
(2026-09-29).

The posting is ``.rhec_detailoffre .ei_mainblocfctl``: the contract/location
fields, then "Qui sommes-nous ?", "Vos missions" and the profile. The same
block ends with "Ces autres offres peuvent vous intéresser" (``.rhec_offres``),
other roles' titles, which would mislead the tagger; drop it.
"""
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .descriptions import _extract_text

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
HOSTS = frozenset({"recrutement.creditmutuel.fr"})


def is_creditmutuel(url: str) -> bool:
    parts = urlsplit(url or "")
    return (parts.netloc.lower() in HOSTS
            and parts.path.lower().endswith("/offre.html")
            and bool(parse_qs(parts.query).get("annonce")))


def description(url: str, session, timeout: int = 15) -> str:
    """Plain-text posting for one Crédit Mutuel offer URL, or "" on any failure."""
    if not is_creditmutuel(url):
        return ""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    node = soup.select_one(".rhec_detailoffre .ei_mainblocfctl")
    if node is None:
        return ""
    for related in node.select(".rhec_offres"):
        related.decompose()
    return _extract_text(str(node))
