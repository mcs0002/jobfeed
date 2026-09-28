"""Euronext server-rendered Drupal careers scraper."""
import concurrent.futures
import hashlib
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

SEARCH_URL = "https://www.euronext.com/en/about/careers/open-positions"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def _fetch(session, page: int):
    response = session.get(
        SEARCH_URL,
        params={"page": page} if page else {},
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    fix_encoding(response)
    return BeautifulSoup(response.text, "html.parser")


def scrape() -> list[dict]:
    session = make_session()
    first = _fetch(session, 0)
    page_numbers = [0]
    for link in first.select(".pagination a[href]"):
        match = re.search(r"[?&]page=(\d+)", link.get("href", ""))
        if match:
            page_numbers.append(int(match.group(1)))
    last_page = max(page_numbers)

    pages = {0: first}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_fetch, session, page): page
            for page in range(1, last_page + 1)
        }
        for future in concurrent.futures.as_completed(futures):
            pages[futures[future]] = future.result()

    jobs = {}
    for page in range(last_page + 1):
        for row in pages[page].select("table.views-table tbody tr"):
            link = row.select_one(
                'td.views-field-field-job-title a[href*="/job-offers/"]'
            )
            if not link:
                continue
            url = urljoin(SEARCH_URL, link.get("href", ""))
            match = re.search(r"/r(\d+)-", url)
            job_id = match.group(1) if match else hashlib.md5(
                url.encode()
            ).hexdigest()[:16]
            country_el = row.select_one(".views-field-field-country")
            city_el = row.select_one(".views-field-name")
            location = ", ".join(
                value for value in (
                    city_el.get_text(" ", strip=True) if city_el else "",
                    country_el.get_text(" ", strip=True) if country_el else "",
                ) if value
            )
            jobs[job_id] = {
                "id": f"euronext_{job_id}",
                "title": link.get_text(" ", strip=True),
                "url": url,
                "location": location,
                "posted": "",
            }
    return list(jobs.values())
