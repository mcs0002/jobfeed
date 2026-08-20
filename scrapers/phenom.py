"""Phenom People server-rendered search scraper."""
import concurrent.futures
import json
import math
import re
from urllib.parse import quote

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def _page(session, config: dict, offset: int) -> dict:
    response = session.get(
        config["search_url"],
        params={"from": offset, "s": 1},
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    fix_encoding(response)
    soup = BeautifulSoup(response.text, "html.parser")
    script = next(
        (
            item.get_text()
            for item in soup.find_all("script")
            if "phApp.ddo" in item.get_text()
        ),
        "",
    )
    marker = "phApp.ddo = "
    if marker not in script:
        raise RuntimeError("Phenom page did not expose its search inventory")
    start = script.index(marker) + len(marker)
    ddo, _ = json.JSONDecoder().raw_decode(script[start:])
    result = ddo.get("eagerLoadRefineSearch", {})
    if result.get("status") != 200:
        raise RuntimeError(f"Phenom search returned status {result.get('status')}")
    return result


def _slug(title: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return quote(value)


def scrape(config: dict) -> list[dict]:
    session = make_session()
    first = _page(session, config, 0)
    total = int(first.get("totalHits", 0))
    first_items = first.get("data", {}).get("jobs", [])
    page_size = len(first_items)
    if total and not page_size:
        raise RuntimeError(f"Phenom reported {total} jobs but returned no jobs")

    offsets = list(range(0, total, page_size or 10))
    pages = {0: first}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=config.get("workers", 6)
    ) as executor:
        futures = {
            executor.submit(_page, session, config, offset): offset
            for offset in offsets[1:]
        }
        for future in concurrent.futures.as_completed(futures):
            pages[futures[future]] = future.result()

    filter_field = config.get("filter_field")
    filter_value = config.get("filter_value")
    expected = total
    if filter_field and filter_value:
        aggregations = first.get("data", {}).get("aggregations", [])
        aggregate = next(
            (
                item.get("value", {})
                for item in aggregations
                if item.get("field") == filter_field
            ),
            {},
        )
        expected = int(aggregate.get(filter_value, -1))
        if expected < 0:
            raise RuntimeError(
                f"Phenom facet {filter_field}={filter_value!r} was not reported"
            )

    base_url = config["base_url"].rstrip("/")
    jobs = {}
    matched_rows = 0
    for offset in offsets:
        for item in pages[offset].get("data", {}).get("jobs", []):
            if filter_field and item.get(filter_field) != filter_value:
                continue
            matched_rows += 1
            job_id = str(item.get("reqId") or item.get("jobId") or "").strip()
            title = item.get("title", "").strip()
            if not job_id or not title:
                continue
            location = (
                item.get("location")
                or item.get("cityStateCountry")
                or item.get("city", "")
            )
            jobs[job_id] = {
                "id": f"phenom_{config.get('source_id', 'company')}_{job_id}",
                "title": title,
                "url": f"{base_url}/job/{job_id}/{_slug(title)}",
                "location": location,
                "posted": (item.get("postedDate") or "")[:10],
            }

    if matched_rows != expected:
        raise RuntimeError(
            f"Phenom reported {expected} rows but parsed {matched_rows}"
        )
    return list(jobs.values())
