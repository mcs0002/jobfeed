"""Cornerstone OnDemand (CSOD) public career-site search scraper.

Used by e.g. the World Bank Group portal at
https://worldbankgroup.csod.com/ux/ats/careersite/1/home?c=worldbankgroup

Flow (fully programmatic, no copied cookies):
1. GET the career-site home page. The HTML shell embeds a short-lived JWT in
   ``csod.context.token`` and the response sets the session cookies the token
   is bound to (the token's ``aud`` claim is the ASP.NET session id).
2. POST the same-origin search service
   ``/services/x/career-site/v1/search`` with ``Authorization: Bearer <token>``
   and paginate via ``pageNumber`` until ``totalCount`` is reached.

Config keys:
    tenant            csod subdomain, e.g. "worldbankgroup" (required)
    site_id           career site id, default 1
    page_size         default 100
    custom_field_dropdowns
                      optional list passed through to the search body, e.g.
                      [{"id": 29, "options": [121]}] filters the World Bank
                      Group portal to Organizations = IFC.
    id_prefix         override the job-id prefix (defaults to csod_<tenant>)
"""
import re
import time

from ._http import make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

_TOKEN_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')


def _iso_date(value: str) -> str:
    """Convert CSOD 'M/D/YYYY' to 'YYYY-MM-DD'; pass through otherwise."""
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", (value or "").strip())
    if not match:
        return value or ""
    month, day, year = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def scrape(config: dict) -> list[dict]:
    tenant = config["tenant"]
    site_id = int(config.get("site_id", 1))
    page_size = int(config.get("page_size", 100))
    dropdowns = config.get("custom_field_dropdowns") or []
    search_text = config.get("search_text", "")
    id_prefix = config.get("id_prefix", f"csod_{tenant}")
    base = f"https://{tenant}.csod.com"
    home_url = f"{base}/ux/ats/careersite/{site_id}/home?c={tenant}"

    session = make_session()
    session.headers.update(HEADERS)

    # Step 1: establish session cookies + extract the embedded JWT.
    home = session.get(home_url, timeout=40)
    home.raise_for_status()
    token_match = _TOKEN_RE.search(home.text)
    if not token_match:
        raise RuntimeError("CSOD home page did not contain a csod.context token")
    session.headers["Authorization"] = f"Bearer {token_match.group(1)}"

    # Step 2: paginate the search service to exhaustion.
    jobs = {}
    total = None
    page_number = 1
    while True:
        response = session.post(
            f"{base}/services/x/career-site/v1/search",
            json={
                "careerSiteId": site_id,
                "careerSitePageId": site_id,
                "pageNumber": page_number,
                "pageSize": page_size,
                "cultureId": 1,
                "searchText": search_text,
                "cultureName": "en-US",
                "states": [],
                "countryCodes": [],
                "cities": [],
                "placeID": "",
                "radius": None,
                "postingsWithinDays": None,
                "customFieldCheckboxKeys": [],
                "customFieldDropdowns": dropdowns,
                "customFieldRadios": [],
            },
            timeout=40,
        )
        response.raise_for_status()
        data = response.json().get("data") or {}
        total = int(data.get("totalCount", 0))
        requisitions = data.get("requisitions") or []
        if not requisitions:
            break
        for requisition in requisitions:
            req_id = str(requisition.get("requisitionId", "")).strip()
            title = (requisition.get("displayJobTitle") or "").strip()
            if not req_id or not title:
                continue
            locations = requisition.get("locations") or []
            parts = []
            for loc in locations:
                city = (loc.get("city") or "").strip()
                country = (loc.get("country") or "").strip()
                piece = ", ".join(p for p in (city, country) if p)
                if piece:
                    parts.append(piece)
            jobs[req_id] = {
                "id": f"{id_prefix}_{req_id}",
                "title": title,
                "url": (
                    f"{base}/ux/ats/careersite/{site_id}/home/requisition/"
                    f"{req_id}?c={tenant}"
                ),
                "location": "; ".join(parts),
                "posted": _iso_date(requisition.get("postingEffectiveDate", "")),
            }
        if len(jobs) >= total or page_number > 200:
            break
        page_number += 1
        time.sleep(0.5)

    if total is None:
        raise RuntimeError("CSOD search returned no data")
    if len(jobs) != total:
        raise RuntimeError(
            f"CSOD reported {total} jobs but parsed {len(jobs)}"
        )
    return list(jobs.values())
