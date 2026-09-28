"""BNP Paribas group careers scraper (paced, session-rotating).

Uses the official `group.bnpparibas` all-job-offers JSON endpoint
(`?json=1&page=N`, 10 offers per page). The endpoint is public and
cookie-free, but the server enforces a per-session request limit of
roughly 200 pages: after ~210 requests on one cookie jar it returns
403 while fresh sessions immediately receive 200. This adapter paces
requests at slightly over one second and proactively rotates to a new
session every 150 pages, respecting the server-side per-session rate
limit. On any 403 it swaps in a fresh session after a short pause,
allowing a full 5,000+ offer sweep to complete in a single run.
"""

import hashlib
import math
import random
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import make_session


BASE_URL = "https://group.bnpparibas/en/careers/all-job-offers"
PAGE_SIZE = 10
PAGE_PAUSE_SECONDS = 1.2
PAGES_PER_SESSION = 150
FORBIDDEN_PAUSES_SECONDS = (15, 30, 60, 120, 180)
HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE_URL,
    "Sec-CH-UA": (
        '"Google Chrome";v="149", "Chromium";v="149", '
        '"Not)A;Brand";v="24"'
    ),
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}


class _RotatingClient:
    """Requests wrapper that swaps in a fresh session on Akamai 403s."""

    def __init__(self):
        self._session = make_session()
        self._pages_on_session = 0

    def _rotate(self):
        self._session.close()
        self._session = make_session()
        self._pages_on_session = 0

    def fetch_page(self, page: int) -> dict:
        if self._pages_on_session >= PAGES_PER_SESSION:
            self._rotate()
        for attempt, forbidden_pause in enumerate(
            (0,) + FORBIDDEN_PAUSES_SECONDS
        ):
            if forbidden_pause:
                time.sleep(forbidden_pause)
                self._rotate()
            response = self._session.get(
                BASE_URL,
                params={
                    "json": "1",
                    "page": page,
                    "form[hint]": "",
                    "form[q]": "",
                    "search_location": "",
                    "form[coordinates]": "",
                },
                headers=HEADERS,
                timeout=30,
            )
            if response.status_code == 403:
                continue
            response.raise_for_status()
            self._pages_on_session += 1
            return response.json()
        raise RuntimeError(
            f"BNP Paribas page {page} still 403 after "
            f"{len(FORBIDDEN_PAUSES_SECONDS)} session rotations"
        )


def _parse_jobs(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for card in soup.select("article.card-offer"):
        link = card.select_one("a.card-link")
        title_element = card.select_one("h3.title-4")
        if not link or not title_element:
            continue

        url = urljoin(BASE_URL, link.get("href", ""))
        title = title_element.get_text(" ", strip=True)
        location_element = card.select_one(".offer-location")
        location = (
            location_element.get_text(" ", strip=True)
            if location_element
            else ""
        )
        job_key = link.get("href") or f"{title}_{location}"
        job_id = hashlib.md5(job_key.encode()).hexdigest()[:16]
        jobs.append({
            "id": f"bnp_{job_id}",
            "title": title,
            "url": url,
            "location": location,
            "posted": "",
        })
    return jobs


def scrape(progress=None) -> list[dict]:
    client = _RotatingClient()
    first_page = client.fetch_page(1)
    total = int(first_page.get("total", 0))
    if total <= 0:
        # This board carries 5,000+ offers; a zero/missing total means the
        # response shape changed, not an empty board. Returning [] here would
        # delist every stored BNP row.
        raise RuntimeError(
            f"BNP Paribas reported total={total} — response shape changed? "
            f"keys: {sorted(first_page)[:8]}")
    page_count = max(1, math.ceil(total / PAGE_SIZE))

    unique = {}
    for job in _parse_jobs(first_page.get("html", "")):
        unique[job["id"]] = job

    for page in range(2, page_count + 1):
        time.sleep(PAGE_PAUSE_SECONDS + random.uniform(0, 0.4))
        for job in _parse_jobs(client.fetch_page(page).get("html", "")):
            unique[job["id"]] = job
        if progress and page % 25 == 0:
            progress(page, page_count, len(unique))

    if progress:
        progress(page_count, page_count, len(unique))
    # Completeness proof (fleet convention, 0.9 band — see citadel/avature):
    # a selector rot in _parse_jobs would otherwise complete a full 10-minute
    # sweep with zero errors and zero jobs, delisting 5,000+ stored rows.
    # The band tolerates board churn during the long sweep.
    if len(unique) < total * 0.9:
        raise RuntimeError(
            f"BNP Paribas sweep incomplete: parsed {len(unique)} unique "
            f"offers vs {total} reported (selector rot?)")
    return list(unique.values())
