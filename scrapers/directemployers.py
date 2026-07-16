"""DirectEmployers/dejobs public search API scraper."""
import os
import re
import sys

from ._http import make_session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .enrich.descriptions import _extract_text  # noqa: E402

API_URL = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
MAX_PAGES = 200


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def scrape(config: dict) -> list[dict]:
    host = config["host"]
    base_url = f"https://{host}"
    page = 1
    jobs = {}
    session = make_session()

    while True:
        response = session.get(
            API_URL,
            params={"page": page},
            headers={**HEADERS, "Accept": "application/json", "X-Origin": host},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()

        for item in payload.get("jobs", []):
            job_id = str(item.get("guid", "")).strip()
            title = item.get("title_exact", "").strip()
            if not job_id or not title:
                continue
            location = item.get("location_exact", "")
            jobs[job_id] = {
                "id": f"dejobs_{job_id}",
                "title": title,
                "url": (
                    f"{base_url}/{_slug(location)}/"
                    f"{item.get('title_slug') or _slug(title)}/{job_id}/job/"
                ),
                "location": location,
                "posted": item.get("date_new", "")[:10],
                # The solr listing carries the full body; the public job page is
                # a JS shell, so capture it here.
                "description": _extract_text(item.get("description", "") or ""),
            }

        pagination = payload.get("pagination", {})
        # The DirectEmployers/dejobs Solr API exposes no server-side total count
        # in its pagination envelope — only has_more_pages.  A band check is not
        # possible; MAX_PAGES exhaustion raises instead of truncating silently.
        if not pagination.get("has_more_pages"):
            break
        page += 1
        if page > MAX_PAGES:
            raise RuntimeError(
                f"DirectEmployers/{host}: pagination exceeded MAX_PAGES={MAX_PAGES};"
                " board may be larger than expected or pager is stuck"
            )

    return list(jobs.values())
