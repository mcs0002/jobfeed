"""RWE first-party careers API scraper."""
from ._http import make_session

API_URL = "https://www.rwe.com/api/jobborse/entities/v1"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
}
PAGE_SIZE = 100


def scrape(config: dict) -> list[dict]:
    company = config["company"]
    jobs = {}
    skip = 0
    total = None
    session = make_session()

    while total is None or skip < total:
        response = session.post(
            API_URL,
            headers=HEADERS,
            json={
                "ExperienceLevel": [],
                "Company": [company],
                "FunctionalArea": [],
                "Country": [],
                "City": [],
                "Keyword": "",
                "FromQueryString": False,
                "FromPersonalization": False,
                "take": PAGE_SIZE,
                "skip": skip,
                "SortType": "Created_tdt desc",
                "LogoContainerId": "",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        total = int(payload.get("TotalCount", 0))
        results = payload.get("Results", [])

        for item in results:
            if item.get("CustomField1") != company:
                continue
            job_id = str(item.get("Id", "")).strip()
            title = item.get("Title", "").strip()
            url = item.get("Url", "").strip()
            if not job_id or not title or not url:
                continue
            jobs[job_id] = {
                "id": f"rwe_{job_id}",
                "title": title,
                "url": url,
                "location": item.get("Location", "").strip(),
                "posted": (item.get("Created") or "")[:10],
            }

        if not results:
            break
        skip += len(results)

    if len(jobs) != total:
        raise RuntimeError(
            f"RWE API reported {total} {company} jobs but parsed {len(jobs)}"
        )
    return list(jobs.values())
