"""
SmartRecruiters public API scraper.
Endpoint: https://api.smartrecruiters.com/v1/companies/{company_id}/postings
No auth required for public postings.
Used by: some European banks and asset managers.
"""
from ._http import assert_complete, make_session

BASE = "https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(company_id: str, query: str | list | None = None) -> list[dict]:
    """query may be a single keyword or a list of keywords; a list is scraped
    query-by-query and unioned (dedup by posting id). Used to positively scope
    finance-island boards (e.g. R+V: Kapitalanlage/Investment/Treasury out of
    a 1k-role board of Vertrieb/IT noise)."""
    url = BASE.format(company_id=company_id)
    queries = query if isinstance(query, list) else [query]
    jobs: dict = {}
    session = make_session()

    for q in queries:
        offset = 0
        limit = 100
        total = None
        fetched_ids: set[str] = set()
        while True:
            params = {"limit": limit, "offset": offset, "status": "PUBLISHED"}
            if q:
                params["q"] = q
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
            before = len(fetched_ids)
            for j in items:
                location = ""
                loc = j.get("location", {})
                if loc:
                    parts = [loc.get("city", ""), loc.get("country", "")]
                    location = ", ".join(p for p in parts if p)
                job_id = str(j["id"])
                fetched_ids.add(job_id)
                jobs[job_id] = {
                    "id": f"sr_{j['id']}",
                    "title": j.get("name", ""),
                    "url": j.get("ref", ""),
                    "location": location,
                    "posted": j.get("releasedDate", "")[:10],
                }
            if len(fetched_ids) == before:
                raise RuntimeError(
                    f"SmartRecruiters/{company_id}: offset={offset} repeated "
                    "a page with no new posting ids"
                )
            offset += limit
            total = data.get("totalFound", 0) or None
            if total is None or offset >= total:
                break

        assert_complete(len(fetched_ids), total,
                        f"SmartRecruiters/{company_id}" + (f" q={q}" if q else ""))
    return list(jobs.values())
