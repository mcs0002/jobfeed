"""Achmea careers scraper (werkenbijachmea.nl) — server-rendered, offset-paged.

The Connexys/Salesforce "werkenbijachmea" site pre-renders its vacancy cards
server-side (the "salesforce" strings are just a CSS icon class, not a JSON
island). It paginates by a ``?o={offset}`` query in steps of 10. We walk offsets
until a page returns no new ``/vacatures/{slug}`` cards. Title from the card
heading, city from the card's ``li.salesforce.location`` metadata chip; detail
pages are server-rendered so bodies fill via the generic enricher (HTTP
strategy). Covers Achmea Investment Management within the group board (the AM
roles are a small slice; the negative filter drops the rest).
"""
import re

from bs4 import BeautifulSoup

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_STEP = 10
_MAX_OFFSET = 500  # safety backstop


def _card_location(anchor) -> str:
    """City for a vacancy, read from its card's ``li.salesforce.location`` chip.
    Walks up from the vacancy link to the nearest ancestor that holds the chip
    (title link and footer link live in the same card). '' if none — some remote
    roles carry no location, which is correct."""
    node = anchor
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        chip = node.select_one("li.salesforce.location") or node.select_one("li.location")
        if chip:
            return chip.get_text(" ", strip=True)
    return ""


def scrape(config: dict) -> list[dict]:
    """config = {"base": "https://www.werkenbijachmea.nl/vacatures", "prefix": "achmea"}"""
    base = config["base"].rstrip("/")
    prefix = config.get("prefix", "achmea")
    session = make_session()

    jobs = {}
    offset = 0
    while offset <= _MAX_OFFSET:
        resp = session.get(f"{base}?o={offset}", headers=HEADERS, timeout=40)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        new = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not re.search(r"/vacatures/[a-z]", href) or "favoriet" in href:
                continue
            slug = href.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            if not slug or slug in jobs:
                continue
            h = a.find(["h2", "h3", "h4"])
            title = (h.get_text(strip=True) if h else a.get_text(" ", strip=True)).strip()
            if not title or title.lower() == "vacatures" or len(title) < 3:
                continue
            jobs[slug] = {
                "id": f"{prefix}_{slug}",
                "title": title,
                "url": href if href.startswith("http") else base.rsplit("/vacatures", 1)[0] + href,
                "location": _card_location(a),
                "description": "",
                "posted": "",
            }
            new += 1
        if new == 0:
            break
        offset += _STEP
    if not jobs:
        raise RuntimeError(f"achmea: no vacancy cards at {base}")
    return list(jobs.values())
