"""KPMG US careers scraper (kpmguscareers.com).

The US site is a custom WordPress theme with no third-party ATS. Its job list
comes from a plain AJAX endpoint that returns JSON with the result cards as an
embedded HTML string plus a total count:

    GET {base_url}{endpoint}?ajax=1&page_type=search&spage=N
    -> {"postings": {"jobs": "<html cards>", "size": 762}, ...}

12 cards/page; paginate spage=1.. until all `size` rows are collected. Each card
is an <a href="/jobdetail/?jobId=NNN"> with the practice area in
`.eyebrow.text-blue`, the clean title in `.h4`, and the full location list in
the `.list-view .text-xs.text-dark-grey` element (prefixed with the area).
Plain HTTP, no auth/WAF.
"""
import re

from bs4 import BeautifulSoup

from ._http import assert_complete, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
DEFAULT_ENDPOINT = (
    "/wp-content/themes/understrap-child-main/page-templates/google/get-jobs.php"
)


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "base_url": "https://www.kpmguscareers.com",
        "endpoint": "/wp-content/.../get-jobs.php",  # optional, defaults above
        "page_size": 12,                              # cards per page
    }
    """
    base_url = config["base_url"].rstrip("/")
    endpoint = config.get("endpoint", DEFAULT_ENDPOINT)
    page_size = config.get("page_size", 12)
    url = f"{base_url}{endpoint}"

    session = make_session()
    jobs = {}
    spage = 1
    total = None
    while True:
        r = session.get(
            url,
            params={"ajax": "1", "page_type": "search", "spage": spage},
            headers=HEADERS, timeout=30,
        )
        r.raise_for_status()
        postings = (r.json() or {}).get("postings", {}) or {}
        if total is None:
            total = postings.get("size", 0)
        cards_html = postings.get("jobs", "")
        if not cards_html:
            # Page 1 has no HTML but server reports a positive total → schema
            # drift or endpoint change; fail loud rather than returning [].
            if spage == 1 and total:
                raise RuntimeError(
                    f"KPMG US: total={total} but page 1 returned no cards HTML"
                )
            break
        soup = BeautifulSoup(cards_html, "html.parser")
        anchors = soup.select('a[href*="jobId="]')
        if not anchors:
            # Selector rot: HTML present but no job links parsed.
            if spage == 1 and total:
                raise RuntimeError(
                    f"KPMG US: total={total} but page 1 parsed zero job anchors"
                    " (selector may have changed)"
                )
            break

        new_on_page = 0
        for a in anchors:
            href = a.get("href", "")
            m = re.search(r"jobId=(\d+)", href)
            if not m:
                continue
            job_id = m.group(1)
            if job_id in jobs:
                continue
            title_el = a.select_one(".h4") or a.select_one(".h5")
            area_el = a.select_one(".eyebrow.text-blue")
            area = area_el.get_text(" ", strip=True) if area_el else ""
            loc_el = a.select_one(".list-view .text-dark-grey.text-xs") \
                or a.select_one(".list-view .text-xs")
            location = loc_el.get_text(" ", strip=True) if loc_el else ""
            # the list-view location is prefixed with the practice area ("Tax | ..")
            if area and location.startswith(area):
                location = location[len(area):].lstrip(" |")
            jobs[job_id] = {
                "id": f"kpmgus_{job_id}",
                "title": title_el.get_text(" ", strip=True) if title_el else "",
                "url": f"{base_url}/jobdetail/?jobId={job_id}",
                "location": location,
                "posted": "",
            }
            new_on_page += 1

        if total and len(jobs) >= total:
            break
        if new_on_page == 0:
            break
        spage += 1
        if spage > 1000:  # runaway guard
            break

    assert_complete(len(jobs), total, "KPMG US")
    return list(jobs.values())
