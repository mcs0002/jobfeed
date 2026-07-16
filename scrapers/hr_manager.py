"""HR-manager.net (Talent Recruiter) hosted board scraper — public JSON.

Talent Recruiter tenants expose their open positions as unauthenticated JSON at
``https://api.hr-manager.net/jobportal.svc/{alias}/positionlist/json/`` —
``{"Items":[{"Id","Name","AdvertisementUrlSecure","PositionLocation":{"Name"},
"ShortDescription", "Published":"/Date(ms+zzzz)/"}]}``. The listing carries a
short ad summary inline (``ShortDescription``); the AdvertisementUrl is the
apply form, not a fuller ad, so we use the summary as the body. Nordic tenants
(e.g. ATP, alias ``atp``).
"""
import re

from ._http import make_session

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Accept": "application/json",
}
_DATE = re.compile(r"/Date\((\d+)")


def _posted(value: str) -> str:
    """Parse HR-manager's ``/Date(1782828379000+0200)/`` to YYYY-MM-DD."""
    match = _DATE.search(value or "")
    if not match:
        return ""
    import datetime
    ms = int(match.group(1))
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def scrape(config: dict) -> list[dict]:
    """config = {"alias": "atp"}"""
    alias = config["alias"]
    url = f"https://api.hr-manager.net/jobportal.svc/{alias}/positionlist/json/"

    resp = make_session().get(url, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    payload = resp.json()
    items = payload.get("Items")
    if not isinstance(items, list):
        raise RuntimeError(f"hr_manager: no 'Items' list for alias {alias}")

    jobs = {}
    for item in items:
        job_id = str(item.get("Id") or "").strip()
        title = (item.get("Name") or "").strip()
        if not job_id or not title:
            continue
        location = ""
        loc = item.get("PositionLocation")
        if isinstance(loc, dict):
            location = (loc.get("Name") or "").strip()
        location = location or (item.get("DepartmentNamePlainText") or "").strip()
        jobs[job_id] = {
            "id": f"hrmanager_{alias}_{job_id}",
            "title": title,
            "url": (item.get("AdvertisementUrlSecure")
                    or item.get("AdvertisementUrl") or "").strip(),
            "location": location,
            "description": (item.get("ShortDescription") or "").strip(),
            "posted": _posted(item.get("Published") or item.get("Created") or ""),
        }
    return list(jobs.values())
