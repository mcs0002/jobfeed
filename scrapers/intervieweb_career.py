"""Intervieweb hosted career page (AJAX list) scraper.

Since ~July 2026 the zinrec.intervieweb.it career pages no longer server-render
the vacancy cards: the page ships an empty ``#vacancyList`` plus a
session-tokened endpoint in the hidden ``#url-for-announces`` input, and the
cards arrive from a ``vacancyListCareer`` POST (see the inline
``researchAnnounces`` JS). This handler replicates that call: GET the career
page with a session, parse the endpoint URL and the ``section`` token from the
page, POST for each page of results, parse the returned HTML fragment.

The fragment uses the same ``.vacancy__*`` card markup the old server-rendered
board used. "Nessun annuncio disponibile" is the platform's genuine-empty
marker, so an empty board returns [] without tripping fail-loud; a non-empty
fragment that parses to zero cards raises (markup drift).

Distinct from ``intervieweb.py``, which reads the public ``annunci.php`` JSON
feed — that needs a per-tenant key we don't have for every firm.
"""
import re

from bs4 import BeautifulSoup

from ._http import make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
}
_URL_RE = re.compile(r'id="url-for-announces" value="([^"]+)"')
_SECTION_RE = re.compile(r"'section':\s*'([^']+)'")
EMPTY_MARKER = "Nessun annuncio disponibile"
MAX_PAGES = 20


def scrape(career_url: str, slug: str = "intervieweb") -> list[dict]:
    session = make_session()
    page = session.get(career_url, headers=HEADERS, timeout=40)
    page.raise_for_status()

    url_m = _URL_RE.search(page.text)
    section_m = _SECTION_RE.search(page.text)
    if not url_m or not section_m:
        raise RuntimeError(
            f"intervieweb_career: announce endpoint/section token not found on {career_url}"
        )
    endpoint = url_m.group(1).replace("&amp;", "&")
    section = section_m.group(1)

    jobs: dict[str, dict] = {}
    for page_no in range(1, MAX_PAGES + 1):
        resp = session.post(
            endpoint,
            data={
                "act1": "vacancyListCareer", "section": section,
                "order": "name", "page": page_no,
                "country": "", "region": "", "function": "",
                "project": "", "text": "", "division": "", "company": "",
            },
            headers={**HEADERS, "X-Requested-With": "XMLHttpRequest",
                     "Referer": career_url},
            timeout=40,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise RuntimeError(
                f"intervieweb_career: vacancyListCareer refused on {career_url}: "
                f"{str(payload.get('data'))[:120]}"
            )
        fragment = payload.get("data", "")
        if EMPTY_MARKER in fragment:
            break  # trusted empty (page 1) / past the last page
        soup = BeautifulSoup(fragment, "html.parser")
        cards = soup.select("div.vacancy__render") or soup.select(".vacancy")
        if not cards:
            raise RuntimeError(
                f"intervieweb_career: fragment from {career_url} has no vacancy "
                "cards and no empty-marker (markup drift?)"
            )
        before = len(jobs)
        for card in cards:
            title_el = card.select_one(".vacancy__title h3") or card.select_one(".vacancy__title")
            link_el = card.select_one(".vacancy__title a") or card.select_one("a[href]")
            if not title_el or not link_el:
                continue
            title = title_el.get_text(strip=True)
            href = link_el.get("href", "").strip()
            if not title or not href:
                continue
            if href.startswith("/"):
                href = "https://zinrec.intervieweb.it" + href
            loc_el = card.select_one(".vacancy__location")
            job_id = f"{slug}_{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')}"
            jobs[job_id] = {
                "id": job_id,
                "title": title,
                "url": href,
                "location": loc_el.get_text(strip=True) if loc_el else "",
                "posted": "",
            }
        if len(jobs) == before:
            break  # page added nothing new — done
    return list(jobs.values())
