"""Yello Enterprise / recsolu job-board scraper.

recsolu boards expose a public JSON search endpoint per tenant:

    GET https://{tenant}.recsolu.com/job_boards/{board_id}/search?page_number={n}

The JSON carries an ``html`` string (a block of ``<li class="search-results__item">``
rows) plus a ``more_requisitions`` bool to drive pagination. 25 jobs/page. Plain
HTTP — no auth, cookies, or impersonation needed.

Gotcha: the obvious ``page`` param is silently ignored (always returns page 1);
the real pagination param is ``page_number``. Loop until ``more_requisitions`` is
false, with a hard page cap as a runaway guard.
"""
import html
import re

from ._http import make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
MAX_PAGES = 40  # runaway guard (25/page → 1000 jobs ceiling)

# One job row: the title anchor (href + text) immediately followed by a <div>
# holding the type/region/city <span>s.
_ROW_RE = re.compile(
    r'<a class="search-results__req_title"[^>]*href="(/jobs/[^?"]+)\?job_board_id=[^"]+"[^>]*>'
    r'(.*?)</a><div>(.*?)</div>',
    re.DOTALL,
)
_SPAN_RE = re.compile(r"<span[^>]*>(.*?)</span>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub("", text)).split())


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/")
    board_id = config["board_id"]
    # Optional server-side full-text scope. Big global early-careers boards
    # (e.g. EY) hold 1000+ all-service-line roles and blow past MAX_PAGES; a
    # `query` narrows them to the relevant slice at the source (finance-island
    # positive scoping, same doctrine as the TalentBrew facet scopes).
    query = config.get("query")
    session = make_session()

    jobs: dict[str, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        url = f"{base_url}/job_boards/{board_id}/search"
        params = {"page_number": page}
        if query:
            params["query"] = query
        resp = session.get(url, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        markup = data.get("html") or ""

        new_on_page = 0
        for path, title_html, meta_html in _ROW_RE.findall(markup):
            job_id = path.rsplit("/", 1)[-1]
            if job_id in jobs:
                continue
            spans = [_clean(s) for s in _SPAN_RE.findall(meta_html)]
            spans = [s for s in spans if s]
            # spans are [type, region, city] — the location-bearing tail.
            location = ", ".join(spans[1:]) if len(spans) > 1 else (
                spans[0] if spans else "")
            jobs[job_id] = {
                "id": f"recsolu_{job_id}",
                "title": _clean(title_html),
                "url": f"{base_url}{path}",
                "location": location,
                "posted": "",
            }
            new_on_page += 1

        if not data.get("more_requisitions") or new_on_page == 0:
            break

    return list(jobs.values())
