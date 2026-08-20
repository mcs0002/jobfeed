"""iCIMS career portal scraper.

iCIMS hosts a paginated HTML job list at
``https://<tenant>.icims.com/jobs/search?ss=1&pr=<page>&in_iframe=1`` — 20 jobs
per page, each an anchor to ``/jobs/<id>/<slug>/job``. There is no public
JSON/RSS feed (the ``format=rss``/``format=json`` variants return empty), so we
walk the pages and parse the anchors until a page yields no new job IDs.

Anchor text is prefixed with the literal label "Job Title " (an accessibility
caption), which we strip. Location isn't reliably in the list row, so it's
left to the inline description-enrichment / tagging pass.
"""
import re

from bs4 import BeautifulSoup

from ._http import make_session

PAGE_SIZE = 20
MAX_PAGES = 50  # safety cap (~1000 roles); real iCIMS boards are far smaller
_JOB_HREF = re.compile(r"/jobs/(\d+)/[^/]+/job")
_TITLE_PREFIX = re.compile(r"^\s*Job Title\s+", re.I)


def scrape(base_url: str) -> list[dict]:
    base_url = base_url.rstrip("/")
    session = make_session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }

    jobs: dict[str, dict] = {}
    for page in range(MAX_PAGES):
        url = f"{base_url}/jobs/search?ss=1&pr={page}&in_iframe=1"
        resp = session.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        new_on_page = 0
        for a in soup.find_all("a", href=True):
            m = _JOB_HREF.search(a["href"])
            if not m:
                continue
            job_id = m.group(1)
            if job_id in jobs:
                continue
            title = _TITLE_PREFIX.sub("", a.get_text(" ", strip=True)).strip()
            if not title:
                continue
            # Canonical URL without the iframe flag.
            href = a["href"].split("?")[0]
            jobs[job_id] = {
                "id": f"icims_{job_id}",
                "title": title,
                "url": href,
                "location": "",
            }
            new_on_page += 1

        # Stop when a page adds nothing (last page reached or empty board).
        if new_on_page == 0:
            break

    return list(jobs.values())
