"""Deutsche Bundesbank server-rendered job board scraper."""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

SEARCH_URL = "https://www.bundesbank.de/action/de/729936/bbksearch"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def _fetch(session, page: int):
    response = session.get(
        SEARCH_URL,
        params={
            "pageNumString": page,
            "query": "",
            "tfi-729938": "",
        },
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
    for link in first.select('a[href*="pageNumString="]'):
        match = re.search(r"pageNumString=(\d+)", link.get("href", ""))
        if match:
            page_numbers.append(int(match.group(1)))
    last_page = max(page_numbers)

    pages = {0: first}
    for page in range(1, last_page + 1):
        pages[page] = _fetch(session, page)

    jobs = {}
    for page in range(last_page + 1):
        for card in pages[page].select("li.resultlist__item"):
            link = card.select_one("a.teasable__link[href]")
            title_el = card.select_one(".teasable__title .h2")
            if not link or not title_el:
                continue
            url = urljoin(SEARCH_URL, link.get("href", ""))
            match = re.search(r"-+(\d+)$", url)
            if not match:
                continue
            info_el = card.select_one(".teasable__info")
            info = " ".join(info_el.stripped_strings) if info_el else ""
            location = info.rsplit("|", 1)[-1].strip() if "|" in info else ""
            job_id = match.group(1)
            jobs[job_id] = {
                "id": f"bundesbank_{job_id}",
                "title": " ".join(title_el.stripped_strings),
                "url": url,
                "location": location,
                "posted": "",
            }

    return list(jobs.values())
