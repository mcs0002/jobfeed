"""Full Altays postings, including requirements omitted from short JSON-LD."""
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .._http import fix_encoding
from ..generic import HEADERS
from .descriptions import _extract_text, note_deadline


def is_altays(url: str) -> bool:
    p = urlsplit(url or "")
    return p.hostname == "recrutement.altays-progiciels.com" and "/offres/" in p.path


def description(url: str, session, timeout: int = 20, out: dict | None = None) -> str:
    response = session.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    fix_encoding(response)
    note_deadline(response.text, out)
    body = BeautifulSoup(response.text, "html.parser").select_one(".row.job, .job__left")
    if body is None:
        return ""
    # Within this posting block, sidebars hold job metadata rather than chrome.
    for aside in body.select("aside"):
        aside.name = "div"
    return _extract_text(str(body))
