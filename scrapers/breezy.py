"""Breezy HR public job-board JSON scraper."""
from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(account: str) -> list[dict]:
    response = make_session().get(
        f"https://{account}.breezy.hr/json",
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    jobs = []
    for item in response.json():
        job_id = str(item.get("id", "")).strip()
        title = item.get("name", "").strip()
        if not job_id or not title:
            continue
        location = item.get("location") or {}
        jobs.append({
            "id": f"breezy_{account}_{job_id}",
            "title": title,
            "url": item.get("url", ""),
            "location": location.get("name", "") if isinstance(location, dict) else "",
            "posted": item.get("published_date", "")[:10],
        })
    return jobs
