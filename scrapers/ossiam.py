"""Ossiam careers scraper (bespoke public JSON, no auth).

Ossiam (Natixis-affiliated quant/systematic AM, Paris) serves its openings as
JSON at ``https://api.ossiam.net/careers/posts?pageIx=1&pageSize=50&language=EN``
— ``{"items":[{"id","title","location","team","contract","description",...}]}``
with the full HTML body inline. No per-job URL is exposed, so the human link is
the careers page. Paged, but the board is tiny (single-digit postings).
"""
from ._http import make_session

API = "https://api.ossiam.net/careers/posts"
CAREERS_URL = "https://www.ossiam.com/EN/careers"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Accept": "application/json",
}


def scrape(language: str = "EN") -> list[dict]:
    session = make_session()
    jobs = {}
    page = 1
    while True:
        resp = session.get(
            API, params={"pageIx": page, "pageSize": 50, "language": language},
            headers=HEADERS, timeout=40)
        resp.raise_for_status()
        payload = resp.json()
        if "items" not in payload:
            raise RuntimeError("ossiam: no 'items' in payload")
        for item in payload["items"]:
            job_id = str(item.get("id") or "").strip()
            title = (item.get("title") or "").strip()
            if not job_id or not title:
                continue
            jobs[job_id] = {
                "id": f"ossiam_{job_id}",
                "title": title,
                "url": CAREERS_URL,
                "location": (item.get("location") or "").strip(),
                "description": (item.get("description") or "").strip(),
                "posted": (item.get("dateOfDrafting") or "").strip()[:10],
            }
        if page >= payload.get("totalPages", 1):
            break
        page += 1
    return list(jobs.values())
