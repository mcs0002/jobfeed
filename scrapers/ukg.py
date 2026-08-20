"""UKG Pro Recruiting (rec.pro.ukg.net) job-board scraper — plain HTTP.

UKG Pro career boards are SPAs, but the search runs through a simple JSON POST.
The board page ({tenant}.rec.pro.ukg.net/{TENANT}/JobBoard/{guid}/) embeds an
ASP.NET antiforgery token (`__RequestVerificationToken` + a `.AspNetCore.
Antiforgery.*` cookie); posting that token to
``/{TENANT}/JobBoard/{guid}/JobBoardView/LoadSearchResults`` returns
``{"opportunities":[{Id,Title,Locations,PostedDate,BriefDescription,...}]}``.
No browser needed. Job page: ``.../OpportunityDetail?opportunityId={Id}``.
"""
import re

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_TOKEN = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')


def _location(locations) -> str:
    if not isinstance(locations, list) or not locations:
        return ""
    loc = locations[0]
    addr = loc.get("Address") or {}
    city = addr.get("City") or addr.get("Line2") or ""
    desc = loc.get("LocalizedDescription") or ""
    return city or desc


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "host": "vaneck.rec.pro.ukg.net",
        "tenant": "VAN1502VEAC",
        "board_guid": "9158c56d-61c9-447f-98b1-4ba0b04ca31d",
    }
    """
    host = config["host"].rstrip("/")
    tenant = config["tenant"]
    guid = config["board_guid"]
    base = f"https://{host}/{tenant}/JobBoard/{guid}/"

    session = make_session()
    page = session.get(base, headers=HEADERS, timeout=40)
    page.raise_for_status()
    m = _TOKEN.search(page.text)
    if not m:
        raise RuntimeError(f"ukg: no antiforgery token on {base}")

    resp = session.post(
        base + "JobBoardView/LoadSearchResults",
        json={"opportunitySearch": {
            "Top": 200, "Skip": 0, "QueryString": "", "OrderBy": [], "Filters": []}},
        headers={**HEADERS, "Content-Type": "application/json",
                 "Referer": base, "X-Requested-With": "XMLHttpRequest",
                 "RequestVerificationToken": m.group(1)},
        timeout=40)
    resp.raise_for_status()
    opportunities = resp.json().get("opportunities")
    if opportunities is None:
        raise RuntimeError(f"ukg: no 'opportunities' from {host}")

    jobs = {}
    for item in opportunities:
        job_id = str(item.get("Id") or "").strip()
        title = (item.get("Title") or "").strip()
        if not job_id or not title:
            continue
        jobs[job_id] = {
            "id": f"ukg_{tenant}_{job_id}",
            "title": title,
            "url": f"{base}OpportunityDetail?opportunityId={job_id}",
            "location": _location(item.get("Locations")),
            "description": (item.get("BriefDescription") or "").strip(),
            "posted": (item.get("PostedDate") or "")[:10],
        }
    return list(jobs.values())
