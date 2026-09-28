"""iCIMS Jibe public jobs API scraper."""
from urllib.parse import urljoin

from ._http import assert_complete, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/") + "/"
    page = 1
    jobs = {}
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
        # Server-side facet scope (e.g. AXA's tags3 entity facet: "GIE AXA"
        # cuts the 1.5k-role group board to the ~12 holding-level ALM/
        # investment roles). Keys are passed through verbatim.
        params.update(config.get("extra_params", {}))
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

        before = len(jobs)
        for item in items:
            data = item.get("data", {})
            job_id = str(data.get("req_id") or data.get("slug") or "").strip()
            title = data.get("title", "").strip()
            if not job_id or not title:
                continue
            jobs[job_id] = {
                "id": f"jibe_{config.get('company_id', 'company')}_{job_id}",
                "title": title,
                "url": urljoin(base_url, f"jobs/{job_id}"),
                "location": data.get("full_location") or data.get("short_location", ""),
                "posted": data.get("posted_date", "")[:10],
            }

        if "totalCount" not in payload:
            raise RuntimeError(
                "Jibe API response missing 'totalCount' field — schema drift"
            )
        total = payload["totalCount"]
        if len(jobs) == before:
            raise RuntimeError(
                f"Jibe: page={page} repeated with no new posting ids"
            )
        if len(jobs) >= total:
            break
        page += 1

    assert_complete(len(jobs), total, "Jibe")
    return list(jobs.values())
