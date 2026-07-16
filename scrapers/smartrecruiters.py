"""
SmartRecruiters public API scraper.
Endpoint: https://api.smartrecruiters.com/v1/companies/{company_id}/postings
No auth required for public postings.
Used by: some European banks and asset managers.
"""
from ._http import assert_complete, make_session

BASE = "https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(company_id: str, query: str | None = None) -> list[dict]:
    url = BASE.format(company_id=company_id)
    jobs = []
    offset = 0
    limit = 100
    total = None
    session = make_session()

    while True:
        params = {"limit": limit, "offset": offset, "status": "PUBLISHED"}
        if query:
            params["q"] = query
        r = session.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        items = data.get("content", [])
        if not items:
            break
        for j in items:
            location = ""
            loc = j.get("location", {})
            if loc:
                parts = [loc.get("city", ""), loc.get("country", "")]
                location = ", ".join(p for p in parts if p)
            jobs.append({
                "id": f"sr_{j['id']}",
                "title": j.get("name", ""),
                "url": j.get("ref", ""),
                "location": location,
                "posted": j.get("releasedDate", "")[:10],
            })
        offset += limit
        total = data.get("totalFound", 0) or None
        if total is None or offset >= total:
            break

    assert_complete(len(jobs), total, f"SmartRecruiters/{company_id}")
    return jobs
