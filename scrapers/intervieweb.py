"""Intervieweb public JSON vacancy feed scraper."""
from datetime import datetime

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(feed_url: str) -> list[dict]:
    response = make_session().get(feed_url, headers=HEADERS, timeout=40)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Intervieweb feed did not return a vacancy list")

    jobs = {}
    for item in payload:
        job_id = str(item.get("id", "")).strip()
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        if not job_id or not title or not url:
            raise RuntimeError("Intervieweb returned an incomplete vacancy record")
        posted = ""
        raw_posted = str(item.get("published", "")).split(" ", 1)[0]
        if raw_posted:
            try:
                posted = datetime.strptime(raw_posted, "%d-%m-%Y").date().isoformat()
            except ValueError:
                posted = raw_posted
        jobs[job_id] = {
            "id": f"intervieweb_{job_id}",
            "title": title,
            "url": url,
            "location": str(item.get("location", "")).strip(),
            "posted": posted,
        }

    if len(jobs) != len(payload):
        raise RuntimeError("Intervieweb feed contained duplicate vacancy IDs")
    return list(jobs.values())
