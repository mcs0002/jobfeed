"""TAL.net / WCN description enricher.

``scrapers/talnet.py`` is a LISTING-only scraper (title + url + location); it
stores no description. The generic GET path in ``enrich_descriptions`` then
fails on these pages for a subtle reason: TAL.net renders the whole vacancy
inside a ``<form class="form-view">`` (it's the application form), and the
generic ``_Stripper`` skips ``<form>`` content as page chrome. So the stripper
keeps only the cookie banner + nav + footer — ~700 chars of boilerplate — and
throws the actual posting away. Every campus role (Bank of America, Evercore,
Nomura, L.E.K., Fidelity early-careers) landed with a stub description, which
starved the Haiku tagger of any signal and scattered their ``area`` tags.

The full posting is server-rendered in ``div#vac_desc`` (the WCN vacancy
description panel). Pull that subtree out of the form wrapper first, then run it
through the shared ``_extract_text`` so paragraph handling matches every other
source.
"""
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .descriptions import _INLINE_WS_RE, _MULTI_NL_RE, _PAD_NL_RE

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
MAX_CHARS = 16000


def is_talnet(url: str) -> bool:
    netloc = urlsplit(url or "").netloc.lower()
    return netloc == "tal.net" or netloc.endswith(".tal.net")


def _clean(node) -> str:
    """Plain text from a bs4 node, normalized like ``_extract_text``.

    We CANNOT route these through ``_extract_text``: the WCN vacancy body is
    rendered inside the application ``<form>``, and that stripper drops
    ``<form>`` content as chrome — so it would return nothing. Pull the text
    with bs4 directly (block tags → newlines) and reuse only the whitespace
    normalizers."""
    text = node.get_text("\n", strip=True)
    text = _INLINE_WS_RE.sub(" ", text)
    text = _PAD_NL_RE.sub("\n", text)
    text = _MULTI_NL_RE.sub("\n\n", text).strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip() + " ..."
    return text


def description(url: str, session, timeout: int = 15) -> str:
    """Plain-text vacancy description for one TAL.net job URL, or "" on any
    failure — callers keep going regardless."""
    if not is_talnet(url):
        return ""
    try:
        r = session.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    # Primary: the WCN vacancy-description panel. Fallback: the rich-text form
    # field(s) that hold the body when the panel id is absent on a tenant.
    node = soup.select_one("#vac_desc")
    if node is not None:
        return _clean(node)
    fields = soup.select("div.type_richtext")
    if not fields:
        return ""
    wrapper = BeautifulSoup("<div></div>", "html.parser").div
    for f in fields:
        wrapper.append(f)
    return _clean(wrapper)
