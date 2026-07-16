"""iCIMS Jibe public jobs API scraper."""
from urllib.parse import urljoin

from ._http import assert_complete, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/") + "/"
    page = 1
    jobs = []
    total = None
    session = make_session()

    while True:
        params = {
            "page": page,
            "limit": config.get("page_size", 20),
            "sortBy": "relevance",
            "descending": "false",
            "internal": "false",
        }
        if config.get("domain"):
            params["domain"] = config["domain"]
        response = session.get(
            urljoin(base_url, "api/jobs"),
            params=params,
            headers=HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("jobs", [])
        if not items:
            break

        for item in items:
            data = item.get("data", {})
            job_id = str(data.get("req_id") or data.get("slug") or "").strip()
            title = data.get("title", "").strip()
            if not job_id or not title:
                continue
            jobs.append({
                "id": f"jibe_{config.get('company_id', 'company')}_{job_id}",
                "title": title,
                "url": urljoin(base_url, f"jobs/{job_id}"),
                "location": data.get("full_location") or data.get("short_location", ""),
                "posted": data.get("posted_date", "")[:10],
            })

        if "totalCount" not in payload:
            raise RuntimeError(
                "Jibe API response missing 'totalCount' field — schema drift"
            )
        total = payload["totalCount"]
        if len(jobs) >= total:
            break
        page += 1

    assert_complete(len(jobs), total, "Jibe")
    return jobs
