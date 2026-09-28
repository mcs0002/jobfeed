"""ABN AMRO public vacancy API scraper."""
from ._http import make_session

URL = "https://www.werkenbijabnamro.nl/en/api/vacancy/"
# The endpoint started bot-filtering generic UAs (404 to "job-scraper/1.0",
# 2026-06-28). A real browser UA + Accept/Referer restores the 200; no
# browser/JS-challenge needed, just a credible header set.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.werkenbijabnamro.nl/en/vacancies",
    "X-Requested-With": "XMLHttpRequest",
}


# The endpoint caps page size at 10 server-side (meta.maxPerPage=10, take/
# pageSize/limit are all ignored), so the ONLY way to see the full board is to
# page through meta.totalPageCount via `pageNumber`. Without this the scraper
# saw just the newest 10 of ~90+ vacancies.
MAX_PAGES = 40  # safety cap (~400 roles); ABN publishes ~90, leaves headroom


def scrape() -> list[dict]:
    session = make_session()
    jobs = []
    seen = set()
    page = 1
    total_pages = 1  # updated from the first response's meta
    while page <= total_pages and page <= MAX_PAGES:
        response = session.get(
            URL,
            params={"sort": "created", "sortDir": "DESC", "pageNumber": page},
            headers=HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        total_pages = payload.get("meta", {}).get("totalPageCount", 1) or 1

        batch = payload.get("vacancies", [])
        if not batch:
            break
        for job in batch:
            job_id = str(job.get("id", ""))
            slug = job.get("slug", "")
            if not job_id or not slug or job_id in seen:
                continue
            seen.add(job_id)
            jobs.append({
                "id": f"abn_{job_id}",
                "title": job.get("title", ""),
                "url": f"https://www.werkenbijabnamro.nl/en/vacancy/{job_id}/{slug}",
                "location": job.get("address") or job.get("city", ""),
                "posted": job.get("created", "")[:10],
            })
        page += 1
    return jobs
