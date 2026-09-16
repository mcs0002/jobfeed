"""Metzler careers scraper (Next.js flight payload, no auth).

Metzler moved off its TYPO3 site around 2026-09-02: the old
``/de/metzler/karriere/stellenangebote`` URL now redirects to
``/de/karriere/offene-stellen``, which renders its vacancy cards client-side.
The full inventory ships in the page's ``self.__next_f.push([1, "..."])``
chunks as two arrays, ``professionalsListings`` and ``studentsListings``, each
item carrying ``vacancyNumber`` (MET-YYMM-NNNN), ``slug``, ``title``, ``city``
and ``listingStart``. Detail pages are server-rendered at
``/de/karriere/offene-stellen/<slug>``.

Ids are keyed on the vacancy number, which is stable across slug edits.
"""
import json
import re

from ._http import make_session

LIST_URL = "https://www.metzler.com/de/karriere/offene-stellen"
DETAIL_BASE = "https://www.metzler.com/de/karriere/offene-stellen"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
ARRAYS = ("professionalsListings", "studentsListings")

_PUSH = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def _flight_text(html: str) -> str:
    return "".join(json.loads(chunk) for chunk in _PUSH.findall(html))


def _arrays(text: str):
    decoder = json.JSONDecoder()
    for name in ARRAYS:
        marker = f'"{name}":'
        start = 0
        while (index := text.find(marker, start)) >= 0:
            start = index + len(marker)
            try:
                value, _ = decoder.raw_decode(text[start:])
            except ValueError:
                continue
            if isinstance(value, list):
                yield value


def scrape() -> list[dict]:
    session = make_session()
    resp = session.get(LIST_URL, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    text = _flight_text(resp.text)
    if not any(name in text for name in ARRAYS):
        raise RuntimeError("metzler: page carries no vacancy listings payload")
    jobs = {}
    for listing in _arrays(text):
        for item in listing:
            if not isinstance(item, dict):
                continue
            vac = (item.get("vacancyNumber") or "").strip()
            title = (item.get("title") or "").strip()
            slug = (item.get("slug") or "").strip()
            if not vac or not title or not slug:
                continue
            jobs[vac] = {
                "id": f"metzler_{vac}",
                "title": title,
                "url": f"{DETAIL_BASE}/{slug}",
                "location": (item.get("city") or "").strip(),
                "description": "",
                "posted": (item.get("listingStart") or "")[:10],
            }
    if not jobs:
        raise RuntimeError("metzler: listings payload parsed to zero vacancies")
    return list(jobs.values())
