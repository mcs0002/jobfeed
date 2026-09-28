"""Alpiq careers scraper (server-rendered TYPO3 job collection, no auth).

Alpiq stopped serving its SuccessFactors career site: ``jobs.alpiq.com``'s TLS
certificate expired 2026-09-08 and was never renewed. The vacancies now render
on www.alpiq.com as a ``successfactors_jobcollection`` content element, four
cards per page, paginated at ``/career/open-jobs/jobs/job-page-N/f1-*/f2-*/search``.

Each ``li.job-item`` carries ``data-job-altid`` (the requisition, ``R-10124``)
and a detail link ``/career/open-jobs/your-application/<n>``. The small ``<n>``
is a CMS index, so ids are keyed on the requisition instead. Detail pages are
server-rendered; the posting body is the first ``.rte-text-wrapper`` inside the
``successfactors_jobdetail`` frame (the others are shared furniture: consent
notice, inclusion statement, address).
"""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import make_session

BASE = "https://www.alpiq.com"
LIST_URL = BASE + "/career/open-jobs"
PAGE_URL = BASE + "/career/open-jobs/jobs/job-page-{n}/f1-%2A/f2-%2A/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
MAX_PAGES = 20
_WORKLOAD = re.compile(r"\s*-\s*\d+%$")


def _cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for item in soup.select("li.job-item"):
        req = (item.get("data-job-altid") or "").strip()
        link = item.select_one("a.title")
        if not req or not link:
            continue
        contract = item.select_one(".contract span")
        jobs.append({
            "id": f"alpiq_{req}",
            "title": link.get_text(" ", strip=True),
            "url": urljoin(BASE, link.get("href", "")),
            "location": (_WORKLOAD.sub("", contract.get_text(" ", strip=True))
                         if contract else ""),
            "description": "",
            "posted": "",
        })
    return jobs


def _description(session, url: str) -> str:
    try:
        resp = session.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception:
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.select_one(
        ".frame-type-successfactors_jobdetail .text-wrapper.rte-text-wrapper")
    return body.get_text(" ", strip=True) if body else ""


def scrape() -> list[dict]:
    session = make_session()
    resp = session.get(LIST_URL, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    if "successfactors_jobcollection" not in resp.text:
        raise RuntimeError("alpiq: page carries no job collection element")
    jobs = {j["id"]: j for j in _cards(resp.text)}
    for n in range(2, MAX_PAGES + 1):
        if f"job-page-{n}/" not in resp.text:
            break
        resp = session.get(PAGE_URL.format(n=n), headers=HEADERS, timeout=40)
        resp.raise_for_status()
        page = _cards(resp.text)
        if not page:
            break
        jobs.update((j["id"], j) for j in page)
    if not jobs:
        raise RuntimeError("alpiq: job collection parsed to zero vacancies")
    for job in jobs.values():
        job["description"] = _description(session, job["url"])
    return list(jobs.values())
