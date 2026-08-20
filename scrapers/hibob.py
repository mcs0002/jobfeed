"""HiBob public careers-site scraper."""
import os
import sys

from ._http import make_session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .enrich.descriptions import _extract_text  # noqa: E402

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}

# The /api/job-ad listing already carries the full posting body, so capture it
# at scrape time rather than re-fetching a JS-shell page per row.
_BODY_FIELDS = ("description", "responsibilities", "requirements", "benefits")


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/")
    headers = {
        **HEADERS,
        "Accept": "application/json",
        "CompanyIdentifier": config["company_identifier"],
        "Referer": f"{base_url}/",
    }
    response = make_session().get(f"{base_url}/api/job-ad", headers=headers, timeout=20)
    response.raise_for_status()

    jobs = []
    for job in response.json().get("jobAdDetails", []):
        job_id = job.get("id", "")
        if not job_id:
            continue
        location = ", ".join(
            value for value in (job.get("site", ""), job.get("country", ""))
            if value
        )
        body = "\n".join(
            job.get(f) or "" for f in _BODY_FIELDS if (job.get(f) or "").strip()
        )
        jobs.append({
            "id": f"hibob_{job_id}",
            "title": job.get("title", ""),
            "url": f"{base_url}/jobs/{job_id}",
            "location": location,
            "posted": job.get("publishedAt", "")[:10],
            "description": _extract_text(body),
        })
    return jobs
