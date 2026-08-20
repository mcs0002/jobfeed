"""Bank for International Settlements vacancy scraper.

BIS moved its careers page to a JSON API (the old React-cache HTML parse broke,
returning 0). Two hops, both plain JSON:

  1. ``/api/document_lists/vacancies.json`` → ``{"list": {"/workday_job/JR...":
     {"path": "/workday_job/jr..."}, ...}}`` — the current vacancy paths.
  2. ``/api{path}.json`` (e.g. ``/api/workday_job/jr100424.json``) → the full
     posting (titles, department, location, apply URL, description).

BIS runs its hiring on Workday but exposes this clean public JSON in front of
it, so no Workday tenant scrape is needed.
"""
from ._http import make_session

BASE = "https://www.bis.org/api"
LIST_URL = f"{BASE}/document_lists/vacancies.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def scrape() -> list[dict]:
    session = make_session()
    resp = session.get(LIST_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    paths = [entry.get("path") for entry in resp.json().get("list", {}).values()]

    jobs = []
    for path in paths:
        if not path:
            continue
        detail = session.get(f"{BASE}{path}.json", headers=HEADERS, timeout=30)
        if not detail.ok:
            continue
        d = detail.json()
        jrid = d.get("job_requisition_id") or path.rsplit("/", 1)[-1]
        title = d.get("short_title") or d.get("long_title") or ""
        if not title:
            continue
        jobs.append({
            "id": f"bis_{jrid}",
            "title": title,
            "url": d.get("external_apply_url")
            or f"https://www.bis.org/{d.get('path', '').lstrip('/')}",
            "location": d.get("location", ""),
            "description": d.get("posting_description", ""),
            "posted": str(d.get("publication_start_date", ""))[:10],
        })
    return jobs
