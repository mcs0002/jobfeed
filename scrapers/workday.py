"""
Workday scraper using their internal JSON API.
Most large banks use Workday. The API endpoint is discoverable by inspecting
network requests on any Workday career page.

Format: POST https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
"""
import concurrent.futures
from urllib.parse import quote

from ._http import assert_complete, make_session

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Workday-Client": "2023.43.4",
}


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "tenant": "goldmansachs",
        "version": "wd1",          # wd1, wd3, wd5 — varies by company
        "board": "External_Career_Site",
        "host": "...",             # optional: full host override (e.g. when
                                   #   the public host is hyphenated but the
                                   #   tenant in the API path is underscored,
                                   #   as with osv-cci vs osv_cci)
        "applied_facets": {        # optional: pre-applied facet filter — e.g.
            "jobFamilyGroup": [    #   isolate one job family on a shared
                "fd2c157204cd0141235ee393c30c641d"  # Workday tenant.
            ]
        },
        "search_text": "Global Advisors",  # optional: full-text query to scope a
                                   #   shared corporate tenant down to one
                                   #   brand (e.g. SSGA on the State Street
                                   #   `Global` board, ~98 of 1350 rows).
    }
    """
    tenant = config["tenant"]
    version = config.get("version", "wd1")
    board = config["board"]
    host = config.get("host") or f"{tenant}.{version}.myworkdayjobs.com"
    base = f"https://{host}"
    api_url = f"{base}/wday/cxs/{tenant}/{board}/jobs"
    applied_facets = config.get("applied_facets") or {}

    jobs = {}
    session = make_session()
    _scrape_search(api_url, base, board, tenant, applied_facets,
                   config.get("search_text", ""), jobs, session)
    return list(jobs.values())


def _scrape_search(api_url, base, board, tenant, applied_facets,
                   search_text, jobs, session):
    """Paginate one searchText query into the shared jobs dict (dedup by path)."""
    limit = 20
    pages = {}

    def fetch(offset):
        payload = {
            "appliedFacets": applied_facets,
            "limit": limit,
            "offset": offset,
            "searchText": search_text,
        }
        response = session.post(
            api_url, json=payload, headers=HEADERS, timeout=20
        )
        response.raise_for_status()
        return response.json()

    first = fetch(0)
    total = int(first.get("total", 0))
    pages[0] = first
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch, offset): offset
            for offset in range(limit, total, limit)
        }
        for future in concurrent.futures.as_completed(futures):
            pages[futures[future]] = future.result()

    fetched = 0
    for offset in sorted(pages):
        for j in pages[offset].get("jobPostings", []):
            fetched += 1
            path = j.get("externalPath", "")
            if not path or path in jobs:
                continue
            jobs[path] = {
                "id": f"wd_{tenant}_{path.split('/')[-1]}",
                "title": j.get("title", ""),
                "url": f"{base}/{quote(board, safe='_-')}{path}",
                "location": j.get("locationsText", ""),
                "posted": j.get("postedOn", ""),
            }

    # Guard against a silent partial (a page fetch dropped a chunk, pagination
    # broke, etc.) which the delister would misread as a shrunken board. Assert
    # on the PRE-dedup fetched count: dedup-by-externalPath and cross-call
    # merging into `jobs` can legitimately pull len(jobs) below `total`, so the
    # meaningful "did we retrieve the whole board" measure is how many postings
    # the API actually handed back, not how many survived dedup.
    assert_complete(fetched, total, f"Workday/{tenant}")
