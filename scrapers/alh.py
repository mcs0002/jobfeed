"""ALH Gruppe (Alte Leipziger – Hallesche, Oberursel) job search API.

``alh.de/job-search/v1/search`` returns 25/page JSON (``pageSize`` is rejected
server-side with API-VAL-1003, so we walk ``meta.totalPages``). Application
links go to a P&I bewerber-web SPA (JS shell — no enrichable description, so
NONE strategy; German titles are descriptive enough for the tagger).
"""
from ._http import make_session

SEARCH_URL = "https://www.alh.de/job-search/v1/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
           "Accept": "application/json"}
MAX_PAGES = 20


def scrape(config: dict | None = None) -> list[dict]:
    session = make_session()
    jobs: dict = {}
    page = 1
    total = None
    while page <= MAX_PAGES:
        resp = session.get(SEARCH_URL, params={"page": page},
                           headers=HEADERS, timeout=40)
        resp.raise_for_status()
        data = resp.json()
        meta = data.get("meta", {})
        if total is None:
            total = int(meta.get("total") or 0)
        for item in data.get("results", []):
            job_id = str(item.get("id") or "").strip()
            title = (item.get("title") or "").strip()
            if not job_id or not title:
                continue
            jobs[job_id] = {
                "id": f"alh_{job_id}",
                "title": title,
                "url": (item.get("url") or "").strip(),
                "location": ", ".join(loc) if isinstance(
                    (loc := item.get("location") or ""), list) else loc.strip(),
                "posted": str(item.get("lastUpdate") or "")[:10],
            }
        if not meta.get("hasNext"):
            break
        page += 1
    if total and len(jobs) < total:
        raise RuntimeError(f"alh: reported {total} jobs but parsed {len(jobs)}")
    if not jobs:
        raise RuntimeError("alh: search returned no jobs (API moved?)")
    return list(jobs.values())
