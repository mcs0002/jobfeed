"""Vienna Insurance Group holding job API (group.vig, Umbraco).

``/umbraco/api/jobapi/getjobslist`` returns the full holding-level list in one
response — but only with XHR-ish headers (a bare GET gets an empty list, not
an error). Task/profile HTML is inline (SCRAPER strategy). Wiener Städtische /
Donau operating-company boards are separate and not covered by this API.
"""
from ._http import make_session

API_URL = "https://group.vig/umbraco/api/jobapi/getjobslist"
CAREERS = "https://group.vig/karriere/bewerben/jobs/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Accept": "application/json",
    "Referer": CAREERS,
    "X-Requested-With": "XMLHttpRequest",
}


def scrape(config: dict | None = None) -> list[dict]:
    session = make_session()
    resp = session.get(API_URL, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    data = resp.json()
    total = int(data.get("totalResults") or 0)
    items = data.get("jobs") or []
    if total and not items:
        raise RuntimeError(
            "vig: totalResults > 0 but jobs list empty (XHR gate changed?)")

    jobs = {}
    for item in items:
        job_id = str(item.get("handle") or "").strip()
        title = (item.get("customTitle") or "").strip()
        if not job_id or not title:
            continue
        body = " ".join(
            (item.get(k) or "").strip()
            for k in ("taskText", "profileText")).strip()
        jobs[job_id] = {
            "id": f"vig_{job_id}",
            "title": title,
            "url": (item.get("applyUrl") or CAREERS).strip(),
            "location": (item.get("customCity") or "").strip(),
            "description": body,
            "posted": str(item.get("publishedAt") or "")[:10],
        }
    if total and len(jobs) < total:
        raise RuntimeError(f"vig: reported {total} jobs but parsed {len(jobs)}")
    return list(jobs.values())
