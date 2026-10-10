"""
Workday scraper using their internal JSON API.
Most large banks use Workday. The API endpoint is discoverable by inspecting
network requests on any Workday career page.

Format: POST https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
"""
import concurrent.futures
from urllib.parse import quote

from ._http import UniquePagination, make_session

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
        "scopes": [                # optional, replaces the two keys above: a
            {"applied_facets": {...}},   # union of scopes, each fetched
            {"search_text": "trading"},  # separately and merged by path. Ids
        ],                         #   in one facet list OR together, but two
                                   #   facets in one scope AND, and a
                                   #   search_text cannot OR with a facet, so
                                   #   "finance categories plus anything
                                   #   mentioning trading" needs two scopes.
                                   #   Use single-word search_text only: the
                                   #   API ORs the words of a phrase.
        # A scope may also carry "exclude_facets": {"jobFamilyGroup": [ids]}:
        # the scope's postings minus those filed under these values. It is a
        # subtraction, not an allow-list, so a posting with no category and a
        # category the firm adds later both stay in (an allow-list dropped an
        # uncategorised M&G internship, 2026-10-10). For boards whose finance
        # roles spread over many categories and only the noise is clean.
    }
    """
    tenant = config["tenant"]
    version = config.get("version", "wd1")
    board = config["board"]
    host = config.get("host") or f"{tenant}.{version}.myworkdayjobs.com"
    base = f"https://{host}"
    api_url = f"{base}/wday/cxs/{tenant}/{board}/jobs"
    scopes = config.get("scopes") or [{
        "applied_facets": config.get("applied_facets"),
        "search_text": config.get("search_text", ""),
    }]

    jobs = {}
    session = make_session()
    for scope in scopes:
        facets = scope.get("applied_facets") or {}
        text = scope.get("search_text", "")
        if not scope.get("exclude_facets"):
            _scrape_search(api_url, base, board, tenant, facets, text, jobs, session)
            continue
        kept, dropped = {}, {}
        _scrape_search(api_url, base, board, tenant, facets, text, kept, session)
        for param, ids in _live_excluded(api_url, session, scope["exclude_facets"]).items():
            if ids:
                _scrape_search(api_url, base, board, tenant, {**facets, param: ids},
                               text, dropped, session)
        for path, job in kept.items():
            if path not in dropped:
                jobs.setdefault(path, job)
    return list(jobs.values())


def _live_excluded(api_url, session, exclude: dict) -> dict:
    """Keep only the excluded ids the board currently lists. An id Workday does
    not recognise may be ignored rather than rejected, and subtracting an
    unfiltered fetch would empty the source; a value with no postings today is
    simply absent. A facet the board no longer offers at all is an error."""
    response = session.post(api_url, json={"appliedFacets": {}, "limit": 1,
                                           "offset": 0, "searchText": ""},
                            headers=HEADERS, timeout=20)
    response.raise_for_status()
    listed = {}

    def walk(facets):
        for f in facets:
            values = f.get("values") or []
            if values and "facetParameter" in values[0]:
                walk(values)
            else:
                listed[f.get("facetParameter")] = {v.get("id") for v in values}

    walk(response.json().get("facets") or [])
    out = {}
    for param, ids in exclude.items():
        if param not in listed:
            raise RuntimeError(f"Workday facet {param!r} not offered; cannot exclude")
        out[param] = [i for i in ids if i in listed[param]]
    return out


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

    guard = UniquePagination(f"Workday/{tenant}")
    for offset in sorted(pages):
        postings = pages[offset].get("jobPostings", [])
        guard.add_page((j.get("externalPath") for j in postings), offset)
        for j in postings:
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

    guard.finish(total)
