"""Talentsoft server-rendered vacancy board scraper."""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(config: dict) -> list[dict]:
    board_url = config["board_url"]
    locale = config.get("locale", 2057)
    session = make_session()
    jobs = {}
    page = 1
    total = None

    while True:
        response = session.get(
            board_url,
            params={"page": page, "LCID": locale},
            headers=HEADERS,
            timeout=40,
        )
        response.raise_for_status()
        fix_encoding(response)
        soup = BeautifulSoup(response.text, "html.parser")
        total_el = soup.select_one("[id$='_Pagination_TotalOffers']")
        total_match = re.search(r"(\d[\d,.]*)", total_el.get_text() if total_el else "")
        if total_match:
            total = int(re.sub(r"[,.]", "", total_match.group(1)))

        page_count = 0
        # Talentsoft ships two card templates: the older "ts-offer-card" (e.g.
        # CA-CIB) and the newer "ts-offer-list-item" (e.g. Amundi). Handle both.
        links = soup.select(
            "a.ts-offer-card__title-link, a.ts-offer-list-item__title-link"
        )
        for link in links:
            href = link.get("href", "")
            match = re.search(r"_(\d+)\.aspx(?:\?|$)", href)
            if not href or not match:
                continue
            card = link.find_parent(class_="ts-offer-card")
            if card is not None:
                details = card.select(".ts-offer-card-content__list li")
                location_parts = [item.get_text(" ", strip=True) for item in details[1:]]
                location = ", ".join(reversed(location_parts))
            else:
                # New layout: location is folded into the description blurb
                # (contract type + entity + country + city).
                item = link.find_parent(class_="ts-offer-list-item")
                desc = item.select_one(".ts-offer-list-item__description") if item else None
                location = desc.get_text(" ", strip=True) if desc else ""
            job_id = match.group(1)
            jobs[job_id] = {
                "id": f"talentsoft_{job_id}",
                "title": link.get_text(" ", strip=True),
                "url": urljoin(board_url, href),
                "location": location,
                "posted": "",
            }
            page_count += 1

        if not page_count or total is None or len(jobs) >= total:
            break
        page += 1

    if total is None:
        raise RuntimeError("Talentsoft did not expose a reported vacancy total")
    if len(jobs) != total:
        raise RuntimeError(f"Talentsoft reported {total} jobs but parsed {len(jobs)}")
    return list(jobs.values())
