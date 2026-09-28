"""d.vinci hosted career portal scraper (jobPublication list API).

d.vinci portals (karriere.<firm>.de) expose ``/jobPublication/list.json`` —
plain JSON, no auth. ``fields=small`` returns id/position/URL plus a
``jobOpening`` block with location and categories. Detail pages are
server-rendered (HTTP description strategy).

Config: ``{"base_url": "https://karriere.continentale.de"}``.
"""
from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
           "Accept": "application/json"}


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/")
    session = make_session()
    resp = session.get(base_url + "/jobPublication/list.json",
                       params={"fields": "small"}, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    payload = resp.json()
    items = (payload if isinstance(payload, list)
             else payload.get("jobPublications")
             or next(iter(payload.values()), []))
    if not items:
        raise RuntimeError(f"dvinci: no jobPublications at {base_url}")

    jobs = {}
    for item in items:
        job_id = str(item.get("id") or "").strip()
        title = (item.get("position") or item.get("pageTitle") or "").strip()
        url = (item.get("jobPublicationURL") or "").strip()
        if not job_id or not title or not url:
            continue
        opening = item.get("jobOpening") or {}
        location = (opening.get("location") or "").strip()
        jobs[job_id] = {
            "id": f"dvinci_{job_id}",
            "title": title,
            "url": url,
            "location": location,
            "posted": str(item.get("startDate") or "")[:10],
        }
    if not jobs:
        raise RuntimeError(
            f"dvinci: publications present but none parsed at {base_url}")
    return list(jobs.values())
