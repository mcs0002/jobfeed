"""
Beesite ATS scraper.
Used by Deutsche Bank and other German/European firms.
API: GET https://api-{tenant}.beesite.de/search/?data={json_payload}
"""
import json
from html import unescape
from urllib.parse import urlsplit

from ._http import assert_complete, make_session


def _workday_route(apply_uri):
    """Some beesite tenants (Deutsche Bank) are a job-board frontend over a
    Workday backend: the listing carries no description and a relative
    PositionURI, but ApplyURI is the real Workday job URL. Map it to a clean
    Workday URL + tenant/board config so the row routes to WorkdayEnricher for
    its description (and gets a working apply link). Returns (None, None) when
    ApplyURI isn't Workday-shaped."""
    if isinstance(apply_uri, list):
        apply_uri = apply_uri[0] if apply_uri else ""
    if not apply_uri or "myworkdayjobs.com" not in apply_uri:
        return None, None
    url = apply_uri.rstrip("/")
    if url.endswith("/apply"):
        url = url[: -len("/apply")]
    parts = urlsplit(url)
    tenant = parts.netloc.split(".")[0]
    segments = [s for s in parts.path.split("/") if s]
    board = segments[0] if segments else ""
    if not tenant or not board:
        return None, None
    return url, {"tenant": tenant, "board": board}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "tenant": "deutschebank",     # subdomain: api-{tenant}.beesite.de
        "endpoint": "search",         # "search" (all jobs) or "graduatesearch"
        "id_field": "ID",             # optional: per-posting key for tenants
                                      # that reuse PositionID across locations
    }
    """
    tenant = config.get("tenant", "")
    endpoint = config.get("endpoint", "search")
    base_url = config.get(
        "base_url", f"https://api-{tenant}.beesite.de/{endpoint}/"
    )
    language = config.get("language", "en")
    search_criteria = config.get("search_criteria", [])
    id_field = config.get("id_field")

    jobs = []
    first_item = 1
    count = 100
    total = 0
    session = make_session()

    while True:
        payload = {
            "LanguageCode": language,
            "SearchParameters": {
                "FirstItem": first_item,
                "CountItem": count,
                "MatchedObjectDescriptor": [
                    "ID", "PositionID", "PositionTitle", "PositionURI",
                    "ApplyURI",
                    "PositionLocation.CityName", "PositionLocation.CountryName",
                    "PublicationStartDate", "PositionOfferingType.Name",
                    "CareerLevel.Name",
                ],
                "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}],
            },
            "SearchCriteria": search_criteria,
        }

        r = session.get(base_url, headers=HEADERS, params={"data": json.dumps(payload)}, timeout=20)
        r.raise_for_status()
        data = r.json()

        result = data.get("SearchResult", {})
        total = result.get("SearchResultCountAll", 0)
        items = result.get("SearchResultItems", [])
        if not items:
            # Page 1 empty but server reports jobs → schema drift or bad response.
            if first_item == 1 and total:
                raise RuntimeError(
                    f"Beesite/{tenant or 'custom'}: total={total} but page 1"
                    " returned no items"
                )
            break

        for item in items:
            obj = item.get("MatchedObjectDescriptor", {})
            locations = obj.get("PositionLocation", [{}])
            loc = locations[0] if locations else {}
            city = loc.get("CityName", "")
            country = loc.get("CountryName", "")
            location = f"{city}, {country}".strip(", ")

            if id_field:
                job_id = obj.get(id_field) or obj.get("PositionID") or first_item
            else:
                job_id = obj.get("PositionID") or obj.get("ID") or first_item
            job = {
                "id": f"beesite_{tenant or 'custom'}_{job_id}",
                "title": unescape(obj.get("PositionTitle", "")),
                "url": obj.get("PositionURI", ""),
                "location": location,
                "posted": obj.get("PublicationStartDate", "")[:10],
            }
            # Workday-backed tenants (Deutsche Bank): swap the dead relative
            # PositionURI for the real Workday URL and stamp _wd so the scan's
            # enrichment fills the description via WorkdayEnricher.
            wd_url, wd_cfg = _workday_route(obj.get("ApplyURI"))
            if wd_url:
                job["url"] = wd_url
                job["_wd"] = wd_cfg
            jobs.append(job)

        first_item += count
        if first_item > total:
            break

    assert_complete(len(jobs), total, f"Beesite/{tenant or 'custom'}")
    return jobs
