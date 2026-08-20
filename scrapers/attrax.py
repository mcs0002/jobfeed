"""Attrax server-rendered vacancy search scraper."""
import math
import re
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(config: dict) -> list[dict]:
    search_url = config["search_url"]
    business_unit = config.get("business_unit")
    session = make_session()
    all_jobs = {}
    matched_jobs = {}
    total = None
    page_count = None
    page = 1

    while True:
        response = session.get(
            search_url,
            params={"page": page},
            headers=HEADERS,
            timeout=40,
        )
        response.raise_for_status()
        fix_encoding(response)
        soup = BeautifulSoup(response.text, "html.parser")
        total_el = soup.select_one(".attrax-pagination__total-results")
        total_match = re.search(r"(\d[\d,]*)", total_el.get_text() if total_el else "")
        if not total_match:
            raise RuntimeError("Attrax did not expose a reported vacancy total")
        total = int(total_match.group(1).replace(",", ""))
        cards = soup.select(".attrax-vacancy-tile[data-jobid]")
        if not cards and total:
            raise RuntimeError(f"Attrax reported {total} jobs but page {page} was empty")

        for card in cards:
            job_id = str(card.get("data-jobid", "")).strip()
            link = card.select_one("a.attrax-vacancy-tile__title")
            if not job_id or link is None:
                continue
            unit_el = card.select_one(
                ".attrax-vacancy-tile__option-business-unit-valueset"
            )
            unit = unit_el.get_text(" ", strip=True) if unit_el else ""
            location_el = card.select_one(
                ".attrax-vacancy-tile__location-freetext .attrax-vacancy-tile__item-value"
            )
            job = {
                "id": f"attrax_{job_id}",
                "title": link.get_text(" ", strip=True),
                "url": urljoin(search_url, link.get("href", "")),
                "location": location_el.get_text(" ", strip=True) if location_el else "",
                "posted": "",
            }
            all_jobs[job_id] = job
            if not business_unit or unit == business_unit:
                matched_jobs[job_id] = job

        if page_count is None:
            page_count = math.ceil(total / max(1, len(cards)))
        if page >= page_count:
            break
        page += 1

    if len(all_jobs) != total:
        if len(all_jobs) < 0.9 * total:
            raise RuntimeError(f"Attrax reported {total} jobs but parsed {len(all_jobs)}")
        print(
            f"WARN Attrax reported {total} jobs but parsed {len(all_jobs)}; "
            "keeping partial results",
            file=sys.stderr,
        )
    return list(matched_jobs.values())
