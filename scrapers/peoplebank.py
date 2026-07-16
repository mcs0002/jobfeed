"""PeopleBank-style server-rendered category scraper."""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}

# Explicit empty-state strings PeopleBank renders inside #results when a
# category genuinely has no openings (e.g. Handelsbanken Capital Markets shows
# "There are currently no jobs matching your search criteria.").
EMPTY_STATE_MARKERS = ("no jobs", "no vacancies", "no results")


def scrape(category_url: str) -> list[dict]:
    session = make_session()
    jobs = {}
    next_url = category_url
    visited = set()
    saw_empty_state = False

    while next_url:
        if next_url in visited:
            raise RuntimeError("PeopleBank pagination loop detected")
        visited.add(next_url)
        response = session.get(next_url, headers=HEADERS, timeout=40)
        response.raise_for_status()
        fix_encoding(response)
        soup = BeautifulSoup(response.text, "html.parser")
        results = soup.select_one("#results")
        if results is None:
            raise RuntimeError("PeopleBank search results container is missing")

        results_text = results.get_text(" ", strip=True).lower()
        if any(marker in results_text for marker in EMPTY_STATE_MARKERS):
            saw_empty_state = True

        regular_lists = results.find_all("ul", class_="jobs", recursive=False)
        for job_list in regular_lists:
            for link in job_list.select("a.in-app[itemprop='url']"):
                href = link.get("href", "")
                match = re.search(r"/(\d+)(?:\?|$)", href)
                title_el = link.select_one(".job-list-title")
                if not href or not match or title_el is None:
                    continue
                location_el = link.select_one("[itemprop='address']")
                posted_el = link.select_one("[itemprop='datePosted']")
                job_id = match.group(1)
                jobs[job_id] = {
                    "id": f"peoplebank_{job_id}",
                    "title": title_el.get_text(" ", strip=True),
                    "url": urljoin(category_url, href),
                    "location": location_el.get_text(" ", strip=True) if location_el else "",
                    "posted": posted_el.get_text(" ", strip=True) if posted_el else "",
                }

        next_link = results.select_one("a[rel='next'], .pagination a.next")
        next_url = urljoin(category_url, next_link["href"]) if next_link else None

    if not jobs:
        if saw_empty_state:
            # The #results container rendered an explicit "no jobs / no
            # vacancies / no results" empty-state — a TRUSTWORTHY zero, not a
            # moved layout. Return [] so the delister retires stale rows.
            return []
        # No rows AND no empty-state marker — the row markup (ul.jobs /
        # a.in-app) moved. There's no trustworthy count field, so raise rather
        # than let [] delist the firm.
        raise RuntimeError(f"peoplebank: no jobs parsed from {category_url}")
    return list(jobs.values())
