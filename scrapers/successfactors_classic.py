"""Classic SAP SuccessFactors career site scraper.

The legacy Career Site (`careerN.successfactors.eu/career?company=X`) — distinct
from the modern RMK/jobs2web front that `successfactors.py` / `successfactors_api.py`
handle — server-renders its full job list in one page when asked for the listing
view:

    GET {base_url}/career?company={company}&career_ns=job_listing_summary&navBarLevel=JOB_SEARCH

No pagination, no tokens. Each job is a table row with the title anchor href
`/job/{slug}/{id}/`, location in `.colLocation`, date in `.colDate`. Every job
renders twice (desktop + `hidden-phone` mobile variant) — dedup by id. Some
tenants 302 to a branded host (jobs.{firm}.com) but still serve this markup.
Plain `requests` works.
"""
import re
from datetime import datetime
from urllib.parse import urljoin

from ._http import fix_encoding, make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def _posted(value: str) -> str:
    value = (value or "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "base_url": "https://career5.successfactors.eu",
        "company": "PartnersGroup",
    }
    """
    base_url = config["base_url"].rstrip("/")
    company = config["company"]
    url = (
        f"{base_url}/career?company={company}"
        "&career_ns=job_listing_summary&navBarLevel=JOB_SEARCH"
    )
    resp = make_session().get(url, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    fix_encoding(resp)

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")

    jobs = {}
    for link in soup.select("a[href*='/job/']"):
        href = link.get("href", "")
        m = re.search(r"/job/[^/]*/(\d+)", href)
        title = link.get_text(" ", strip=True)
        if not m or not title:
            continue
        job_id = m.group(1)
        if job_id in jobs:
            continue
        row = link.find_parent("tr")
        loc_el = row.select_one(".colLocation") if row else None
        date_el = row.select_one(".colDate") if row else None
        jobs[job_id] = {
            "id": f"sfclassic_{job_id}",
            "title": title,
            "url": urljoin(resp.url, href),
            "location": loc_el.get_text(" ", strip=True) if loc_el else "",
            "posted": _posted(date_el.get_text(" ", strip=True) if date_el else ""),
        }
    if not jobs:
        # The listing page fetched but no /job/{slug}/{id}/ anchors matched —
        # this classic view moved (or the tenant switched to the modern RMK
        # front). Raise rather than return [] and delist the firm.
        raise RuntimeError(f"successfactors_classic: no jobs parsed for {company}")
    return list(jobs.values())
