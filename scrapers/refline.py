"""Refline (refline.ch) hosted job-board scraper — server-rendered HTML list.

Refline tenants publish a static, server-rendered results page at
``https://apply.refline.ch/{tenant}/search.html``. Each opening is an anchor
``/{tenant}/{jobId}/pub/{n}`` whose text is the job title. The list carries no
body or structured location, so the description is filled by the generic
server-rendered enricher from the (also server-rendered) detail page
(coverage strategy = HTTP). Used for ZKB / Swisscanto (tenant 792841).
"""
import re

from bs4 import BeautifulSoup

from ._http import make_session, fix_encoding

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(config: dict) -> list[dict]:
    """config = {"tenant": "792841"}"""
    tenant = str(config["tenant"])
    url = f"https://apply.refline.ch/{tenant}/search.html"
    pattern = re.compile(rf"/{tenant}/(\d+)/pub/")

    resp = make_session().get(url, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    fix_encoding(resp)
    soup = BeautifulSoup(resp.text, "html.parser")

    jobs = {}
    for anchor in soup.find_all("a", href=True):
        match = pattern.search(anchor["href"])
        if not match:
            continue
        job_id = match.group(1)
        title = anchor.get_text(" ", strip=True)
        if not title or job_id in jobs:
            continue
        href = anchor["href"]
        jobs[job_id] = {
            "id": f"refline_{tenant}_{job_id}",
            "title": title,
            "url": href if href.startswith("http") else f"https://apply.refline.ch{href}",
            "location": "",
            "description": "",
            "posted": "",
        }
    if not jobs:
        # Fetched the results page but no /{tenant}/{id}/pub/ anchors matched —
        # the tenant moved or the page is a shell. Raise rather than return []
        # (which the delister reads as "board empty" and purges the firm).
        raise RuntimeError(f"refline: no jobs parsed for tenant {tenant}")
    return list(jobs.values())
