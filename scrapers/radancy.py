"""Radancy/TalentBrew server-rendered filtered search scraper."""
import math
import re
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(search_url: str) -> list[dict]:
    session = make_session()
    jobs = {}
    page = 1
    total = None
    page_size = None

    while True:
        separator = "&" if "?" in search_url else "?"
        response = session.get(
            f"{search_url}{separator}p={page}",
            headers=HEADERS,
            timeout=40,
        )
        response.raise_for_status()
        fix_encoding(response)
        soup = BeautifulSoup(response.text, "html.parser")
        results = soup.select_one("#search-results")
        if results is None:
            raise RuntimeError("Radancy search results container is missing")
        total = int(results.get("data-total-job-results", 0))
        page_size = int(results.get("data-records-per-page", 0))
        for link in results.select("a.search-results-list__job-link[data-job-id]"):
            job_id = str(link.get("data-job-id", "")).strip()
            item = link.find_parent("li")
            location_el = (
                item.select_one(".job-list-01-list__job-info--location span")
                if item else None
            )
            jobs[job_id] = {
                "id": f"radancy_{job_id}",
                "title": link.get_text(" ", strip=True),
                "url": urljoin(search_url, link.get("href", "")),
                "location": location_el.get_text(" ", strip=True) if location_el else "",
                "posted": "",
            }
        if page >= math.ceil(total / max(1, page_size)):
            break
        page += 1

    if len(jobs) != total:
        if len(jobs) < 0.9 * total:
            raise RuntimeError(f"Radancy reported {total} jobs but parsed {len(jobs)}")
        print(
            f"WARN Radancy reported {total} jobs but parsed {len(jobs)}; "
            "keeping partial results",
            file=sys.stderr,
        )
    return list(jobs.values())
