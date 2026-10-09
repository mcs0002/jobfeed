"""Generali careers scraper (bespoke group JSON, no auth).

The Generali group careers site (gogenerali.com) exposes ALL group openings —
~350 across every Generali entity — as one unauthenticated JSON feed:
``/rest-api/talent-farm-mediator/get-available-jobs-from-ats?allData=true&langCode=en``.
We want only the investment/asset-management entities, so the caller passes
``company_match`` substrings and we filter ``companyName`` client-side (default:
Generali Investments Holding + Generali Asset Management, ~35 roles).

Each item carries the full body inline (``descriptionInternalHTML`` +
``internalQualificationHTML``). Job page: ``/home/jobs/{contestId}``.
"""
from ._http import make_session

API = ("https://gogenerali.com/rest-api/talent-farm-mediator/"
       "get-available-jobs-from-ats?allData=true&langCode=en")
JOB_URL = "https://gogenerali.com/home/jobs/{cid}"
DEFAULT_MATCH = ("Generali Investments", "Generali Asset Management")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Accept": "application/json",
}


def scrape(config: dict | None = None) -> list[dict]:
    """
    config = {"company_match": ["Generali Investments", "Generali Asset Management"]}
    """
    config = config or {}
    matches = tuple(config.get("company_match") or DEFAULT_MATCH)

    resp = make_session().get(API, headers=HEADERS, timeout=45)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("response")
    if not isinstance(rows, list):
        raise RuntimeError("generali: no 'response' list in payload")

    jobs = {}
    for item in rows:
        name = item.get("companyName") or ""
        if not any(m in name for m in matches):
            continue
        cid = str(item.get("contestId") or "").strip()
        title = (item.get("title") or "").strip()
        if not cid or not title:
            continue
        body = " ".join(p for p in (
            item.get("descriptionInternalHTML"),
            item.get("internalQualificationHTML"),
        ) if p).strip()
        jobs[cid] = {
            "id": f"generali_{cid}",
            "title": title,
            "url": JOB_URL.format(cid=cid),
            "location": (item.get("primaryLocation") or "").strip(),
            "description": body,
            "posted": (item.get("currentStatusDate") or "").strip()[:10],
        }
    return list(jobs.values())
