"""VERBUND careers-site API scraper.

jobs.verbund.com renders its "Offene Stellen" list from
/api/joblist/GetJobsOnlyFy (an Umbraco module over VERBUND's onlyfy tenant).
The onlyfy board itself (verbund.onlyfy.jobs) shows no openings to an
anonymous visitor, so this API is the only public listing. Rows link to
onlyfy job pages, which server-render the full posting.
"""
import re

from ._http import make_session

API_URL = "https://jobs.verbund.com/api/joblist/GetJobsOnlyFy"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_ID_RE = re.compile(r"/job/([A-Za-z0-9]+)")
MAX_PAGES = 50


def scrape(config: dict) -> list[dict]:
    session = make_session()
    jobs, page, pages, total = {}, 1, 1, None
    while page <= min(pages, MAX_PAGES):
        r = session.get(API_URL, headers=HEADERS, timeout=30, params={
            "moduleId": config["module_id"],
            "culture": config.get("culture", "de-AT"),
            "currentPage": page,
            "currentSearchword": "",
            "currentCountries": "",
            "currentLocations": "",
            "currentEmploymentTypes": "",
            "currentWorkAreas": "",
            "currentRegions": "",
            "reset": "false",
        })
        r.raise_for_status()
        data = r.json().get("data")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or "resultsCount" not in data:
            raise RuntimeError("VERBUND: malformed joblist payload")
        pages = int(data.get("pages") or 0)
        if pages > MAX_PAGES:
            raise RuntimeError("VERBUND: pagination limit exceeded")
        total = int(data.get("resultsCount") or 0)
        for item in data.get("jobs") or []:
            url = (item.get("link") or "").strip()
            title = (item.get("title") or "").strip()
            m = _ID_RE.search(url)
            if not (url and title and m):
                continue
            place = ", ".join(p for p in (
                ", ".join(item.get("locations") or []),
                (item.get("country") or "").strip()) if p)
            jobs[m.group(1)] = {
                "id": f"verbund_{m.group(1)}",
                "title": title,
                "url": url,
                "location": place,
                "posted": "",
            }
        page += 1

    if total is not None and len(jobs) != total:
        raise RuntimeError(f"VERBUND API reported {total} jobs but parsed {len(jobs)}")
    return list(jobs.values())
