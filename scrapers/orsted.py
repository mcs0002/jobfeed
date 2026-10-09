"""Ørsted first-party careers API scraper.

orsted.com/en/careers/vacancies-list renders its table from a JSON POST to
/api/jobs/jobList. The request body is read off the page's `.career-list`
data attributes (`item`, `countryFilterId`) — the same values the site's
career-list bundle sends. The endpoint sits behind Azure WAF, which 403s a
bare UA and passes the browser header set below. The XML sitemap is no
substitute: it lists ~2x the live board (closed vacancies linger there), so
roles would never delist.

`areas` (optional) keeps only the listed AreaOfWork values, plus rows with no
area; the board is mostly offshore-wind operations and IT.
"""
import re

from ._http import make_session

API_URL = "https://orsted.com/api/jobs/jobList"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://orsted.com",
    "Referer": "https://orsted.com/en/careers/vacancies-list",
}
PAGE_SIZE = 150
MAX_PAGES = 40
_ID_RE = re.compile(r"/(\d+)-[^/]+/?$")


def scrape(config: dict) -> list[dict]:
    areas = set(config.get("areas") or [])
    session = make_session()
    rows, start, total = [], 0, None
    while total is None or start < total:
        r = session.post(API_URL, headers=HEADERS, timeout=30, json={
            "offset": 0,
            "start": start,
            "length": PAGE_SIZE,
            "item": config["item"],
            "countryFilterID": config["country_filter_id"],
            "category": "",
            "employmenttype": "",
            "country": "",
            "location": "",
            "jobtitle": "",
        })
        r.raise_for_status()
        payload = r.json()
        if not isinstance(payload.get("data"), list) or "recordsFiltered" not in payload:
            raise RuntimeError("Ørsted: malformed jobList payload")
        total = int(payload["recordsFiltered"])
        page = payload.get("data") or []
        if not page:
            break
        if start >= MAX_PAGES * PAGE_SIZE:
            raise RuntimeError("Ørsted: pagination limit exceeded")
        rows += page
        start += len(page)

    if len(rows) != total:
        raise RuntimeError(f"Ørsted API reported {total} jobs but returned {len(rows)}")

    parsed_ids = set()
    jobs = {}
    for item in rows:
        url = (item.get("Link") or "").strip()
        title = (item.get("JobName") or "").strip()
        m = _ID_RE.search(url)
        if not (url and title and m):
            raise RuntimeError("Ørsted: vacancy missing title/link/id")
        if m.group(1) in parsed_ids:
            raise RuntimeError("Ørsted: duplicate vacancy across pages")
        parsed_ids.add(m.group(1))
        area = (item.get("AreaOfWork") or "").strip()
        if areas and area and area not in areas:
            continue
        place = ", ".join(p for p in (
            (item.get("PositionWorkPlace") or "").strip(),
            (item.get("PositionLocation") or "").strip()) if p)
        jobs[m.group(1)] = {
            "id": f"orsted_{m.group(1)}",
            "title": title,
            "url": url,
            "location": place,
            "posted": "",
        }
    return list(jobs.values())
