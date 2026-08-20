import math
import re
from datetime import datetime
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def _posted_date(value: str) -> str:
    for date_format in ("%d/%m/%Y", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, date_format).date().isoformat()
        except (TypeError, ValueError):
            pass
    return value or ""


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/")
    locale = config.get("locale", "en_US")
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=None,
    )))
    page = session.get(f"{base_url}/search/", headers=HEADERS, timeout=30)
    page.raise_for_status()
    # Older Career Site Builder tenants embed the token as a bare JS var;
    # newer ones (Janus Henderson, Fortum — checked 2026-07-01) only emit it
    # inside a jQuery $.ajaxSetup() headers block instead. Try both; some
    # tenants (Standard Chartered) still carry both forms.
    token_match = (re.search(r'var CSRFToken = "([^"]+)"', page.text)
                   or re.search(r'"X-CSRF-Token"\s*:\s*"([^"]+)"', page.text))
    if not token_match:
        raise ValueError("SuccessFactors search page did not expose a CSRF token")

    headers = {
        **HEADERS,
        "Content-Type": "application/json",
        "X-CSRF-Token": token_match.group(1),
    }
    jobs = {}
    page_number = 0
    page_count = 1

    while page_number < page_count:
        response = session.post(
            f"{base_url}/services/recruiting/v1/jobs",
            headers=headers,
            json={
                "locale": locale,
                "pageNumber": page_number,
                "sortBy": "",
                "keywords": config.get("keywords", ""),
                "location": "",
                "facetFilters": config.get("facet_filters", {}),
                "brand": "",
                "skills": [],
                "categoryId": config.get("category_id", 0),
                "alertId": None,
                "rcmCandidateId": None,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("jobSearchResult", [])
        if page_number == 0:
            total = int(payload.get("totalJobs", 0))
            page_count = max(1, math.ceil(total / max(1, len(results))))

        for item in results:
            job = item.get("response", {})
            job_id = str(job.get("id", "")).strip()
            title = job.get("unifiedStandardTitle", "").strip()
            if not job_id or not title:
                continue
            url_title = quote(job.get("urlTitle", ""), safe="%")
            location = job.get("jobLocationShort", [])
            jobs[job_id] = {
                "id": f"sfapi_{job_id}",
                "title": title,
                "url": (
                    f"{base_url}/job/{url_title}/{job_id}-{locale}/"
                ),
                "location": location[0].strip() if location else "",
                "posted": _posted_date(job.get("unifiedStandardStart", "")),
            }
        page_number += 1

    return list(jobs.values())
