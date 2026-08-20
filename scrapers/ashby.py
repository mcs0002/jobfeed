"""Ashby scraper using their public posting API.

Ashby exposes a clean JSON board with no auth:
    GET https://api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=true
returns {"jobs": [ {title, locationName, jobUrl, ...}, ... ]}.

The org slug is whatever follows jobs.ashbyhq.com/<slug>; note it sometimes
literally contains a dot (Kraken's slug is "kraken.com").
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .enrich.descriptions import _extract_text  # noqa: E402
from ._http import make_session  # noqa: E402

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Accept": "application/json",
}


def scrape(slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    response = make_session().get(
        url, params={"includeCompensation": "true"}, headers=HEADERS, timeout=30
    )
    response.raise_for_status()
    data = response.json()

    jobs = []
    for j in data.get("jobs", []):
        job_id = j.get("id") or j.get("jobId") or ""
        jobs.append({
            "id": f"ashby_{slug}_{job_id}",
            "title": j.get("title", ""),
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "location": j.get("locationName") or j.get("location", ""),
            "posted": j.get("publishedDate", ""),
            # The posting API returns the full body inline (descriptionHtml);
            # the public job page is a JS shell, so capture it here.
            "description": _extract_text(j.get("descriptionHtml", "") or ""),
        })
    return jobs
