"""Glencore first-party Magnolia careers API scraper."""
from ._http import make_session


BASE_URL = "https://www.glencore.com"
API_URL = f"{BASE_URL}/.rest/api/v2/careers/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(keyword: str = "") -> list[dict]:
    """Scrape the Glencore Magnolia careers API. `keyword` positively scopes the
    feed server-side: the board is the mining/refinery-ops body (~298 roles),
    so keyword="trading" isolates the trading-arm footprint (~14) — Glencore is
    a finance-ISLAND firm, no division facet, the keyword is the only lever."""
    jobs = {}
    offset = 0
    page_size = 100
    total = None
    session = make_session()

    while total is None or len(jobs) < total:
        response = session.get(
            API_URL,
            params={
                "locale": "en",
                "sortBy": "title-asc",
                "offset": offset,
                "limit": page_size,
                "searchCriteria": '{"commodity":["!KCC"]}',
                "keyword": keyword,
            },
            headers=HEADERS,
            timeout=40,
        )
        response.raise_for_status()
        payload = response.json()
        if total is None:
            total = int(payload["totalResults"])
        results = payload.get("data", [])
        if not results and offset < total:
            raise RuntimeError(f"Glencore page at offset {offset} was empty")

        for item in results:
            source_id = str(item.get("id", "")).strip()
            job_id = str(item.get("jobId", "")).strip()
            title = str(item.get("title", "")).strip()
            if not source_id or not job_id or not title:
                continue
            location = ", ".join(
                value for value in (
                    item.get("city", ""),
                    item.get("region", ""),
                    item.get("country", ""),
                )
                if value and value != "\u200b"
            )
            jobs[source_id] = {
                "id": f"glencore_{source_id}",
                "title": title,
                "url": f"{BASE_URL}/en/careers/jobs/{job_id}",
                "location": location,
                "posted": str(item.get("startDate", "")),
            }
        offset = page_size - 1 if offset == 0 else offset + page_size

    if len(jobs) != total:
        raise RuntimeError(f"Glencore reported {total} jobs but parsed {len(jobs)}")
    return list(jobs.values())
