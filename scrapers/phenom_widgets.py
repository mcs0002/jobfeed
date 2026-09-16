"""Phenom People /widgets refineSearch API scraper.

Unlike scrapers/phenom.py (which parses the server-rendered phApp.ddo
inventory and filters locally), this scraper POSTs directly to the public
``/widgets`` endpoint with ``ddoKey: refineSearch`` and a server-side
``selected_fields`` facet filter.  The endpoint is cookie-free: no session,
CSRF token, or browser is required.

This matters for group portals such as careers.allianz.com where the
server-rendered inventory omits rows (local unit filtering returned 43 of a
reported 53), while the filtered widget request returns the exact facet
total.

Config keys:
    widgets_url      e.g. "https://careers.allianz.com/widgets"
    base_url         job-link base, e.g. "https://careers.allianz.com/global/en"
    source_id        short slug used in the stable job id
    selected_fields  facet filter dict, e.g. {"unit": ["Allianz Global Investors"]}
    lang             Phenom locale (default "en_global")
    country          Phenom country (default "global")
    page_size        rows per request (default 50)
"""
import re
import sys
import time
from urllib.parse import quote

from ._http import make_session

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Content-Type": "application/json",
}
PAGE_DELAY_SECONDS = 1.0


def _page(session, config: dict, offset: int, size: int) -> dict:
    body = {
        "lang": config.get("lang", "en_global"),
        "deviceType": "desktop",
        "country": config.get("country", "global"),
        "pageName": "search-results",
        "ddoKey": "refineSearch",
        "sortBy": "",
        "subsearch": "",
        "from": offset,
        "jobs": True,
        "counts": True,
        "all_fields": config.get("all_fields", list(config.get("selected_fields", {}))),
        "size": size,
        "clearAll": False,
        "jdsource": "facets",
        "isSliderEnable": False,
        "pageId": "page10",
        "siteType": "external",
        "keywords": config.get("keywords", ""),
        "global": True,
        "selected_fields": config.get("selected_fields", {}),
        "locationData": {},
    }
    response = session.post(
        config["widgets_url"], json=body, headers=HEADERS, timeout=30
    )
    response.raise_for_status()
    result = response.json().get("refineSearch", {})
    if result.get("status") != 200:
        raise RuntimeError(
            f"Phenom widgets returned status {result.get('status')}"
        )
    return result


def _slug(title: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return quote(value)


def scrape(config: dict) -> list[dict]:
    size = int(config.get("page_size", 50))
    session = make_session()
    first = _page(session, config, 0, size)
    total = int(first.get("totalHits", 0))

    rows = list(first.get("data", {}).get("jobs", []))
    if total and not rows:
        raise RuntimeError(f"Phenom widgets reported {total} jobs but returned none")

    while len(rows) < total:
        time.sleep(PAGE_DELAY_SECONDS)
        page = _page(session, config, len(rows), size)
        batch = page.get("data", {}).get("jobs", [])
        if not batch:
            print(
                f"WARN Phenom widgets pagination stalled at {len(rows)} of "
                f"{total} rows; keeping partial results",
                file=sys.stderr,
            )
            break
        rows.extend(batch)

    if len(rows) != total:
        if len(rows) < 0.9 * total:
            raise RuntimeError(
                f"Phenom widgets reported {total} rows but returned {len(rows)}"
            )
        print(
            f"WARN Phenom widgets reported {total} rows but returned {len(rows)}; "
            "keeping partial results",
            file=sys.stderr,
        )

    # When filtering on a single facet value, cross-check the server-side
    # facet aggregate against totalHits so a silently ignored filter fails
    # loudly instead of returning the whole group feed.
    selected = config.get("selected_fields", {})
    single = [(f, v[0]) for f, v in selected.items() if len(v) == 1]
    for field, value in single:
        # Schema drift guard: if the field name vanished from EVERY row, the
        # row-level cross-check can't say anything (the server-side facet still
        # ran), so skip it rather than failing every row on a renamed field.
        if all(row.get(field) is None for row in rows):
            print(
                f"WARN Phenom facet field {field!r} absent from all rows; "
                "skipping cross-check",
                file=sys.stderr,
            )
            continue
        mismatched = [row for row in rows if row.get(field) != value]
        if mismatched:
            if len(mismatched) > 0.1 * len(rows):
                raise RuntimeError(
                    f"Phenom widgets returned {len(mismatched)} rows where "
                    f"{field} != {value!r}; the server-side filter was not applied"
                )
            print(
                f"WARN Phenom widgets returned {len(mismatched)} rows where "
                f"{field} != {value!r}; keeping partial results",
                file=sys.stderr,
            )
        aggregations = first.get("data", {}).get("aggregations", [])
        aggregate = next(
            (
                item.get("value", {})
                for item in aggregations
                if item.get("field") == field
            ),
            None,
        )
        if aggregate is not None:
            expected = int(aggregate.get(value, -1))
            if expected != total:
                if expected < 0 or abs(expected - total) > 0.1 * total:
                    raise RuntimeError(
                        f"Phenom facet {field}={value!r} reports {expected} jobs "
                        f"but totalHits was {total}"
                    )
                print(
                    f"WARN Phenom facet {field}={value!r} reports {expected} jobs "
                    f"but totalHits was {total}; keeping partial results",
                    file=sys.stderr,
                )

    base_url = config["base_url"].rstrip("/")
    jobs = {}
    for item in rows:
        job_id = str(item.get("reqId") or item.get("jobId") or "").strip()
        title = (item.get("title") or "").strip()
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
    return list(jobs.values())
