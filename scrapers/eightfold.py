"""Eightfold public careers API scraper."""
import concurrent.futures
from datetime import datetime, timezone

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/")
    domain = config["domain"]
    page_size = 10
    jobs = {}
    session = make_session()

    def fetch(start):
        response = session.get(
            f"{base_url}/api/apply/v2/jobs",
            params={
                "domain": domain,
                "start": start,
                "num": page_size,
                "sort_by": "relevance",
            },
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    first = fetch(0)
    total = int(first.get("count", 0))
    pages = {0: first}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch, start): start
            for start in range(page_size, total, page_size)
        }
        for future in concurrent.futures.as_completed(futures):
            pages[futures[future]] = future.result()

    for start in sorted(pages):
        payload = pages[start]
        positions = payload.get("positions", [])

        for position in positions:
            job_id = str(position.get("id", "")).strip()
            title = (
                position.get("posting_name")
                or position.get("name")
                or ""
            ).strip()
            if not job_id or not title:
                continue
            timestamp = position.get("t_create") or position.get("t_update")
            posted = ""
            if timestamp:
                posted = datetime.fromtimestamp(
                    timestamp, tz=timezone.utc
                ).date().isoformat()
            jobs[job_id] = {
                "id": f"eightfold_{domain}_{job_id}",
                "title": title,
                "url": (
                    position.get("canonicalPositionUrl")
                    or f"{base_url}/careers/job/{job_id}"
                ),
                "location": position.get("location", ""),
                "posted": posted,
            }

    return list(jobs.values())
