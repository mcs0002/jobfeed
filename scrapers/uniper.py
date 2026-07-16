"""Uniper first-party job filter API scraper."""
import requests

from ._http import make_session


BASE_URL = "https://careers.uniper.energy"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape() -> list[dict]:
    jobs = {}
    page = 0
    total = None
    session = make_session()

    while total is None or len(jobs) < total:
        response = session.post(
            f"{BASE_URL}/api/filter/query",
            json={"locale": "en", "page": page, "filter": {}, "searchQuery": ""},
            headers=HEADERS,
            timeout=40,
        )
        response.raise_for_status()
        payload = response.json()
        if total is None:
            total = int(payload.get(
                "totalHits",
                payload.get("total", payload.get("totalResults", 0)),
            ))
        results = payload.get("jobs", payload.get("data", []))
        if not results:
            break

        for item in results:
            data = item.get("data", item)
            job_id = str(data.get(
                "idClient",
                data.get("id", data.get("jobId", "")),
            )).strip()
            title = str(data.get("title", "")).strip()
            if not job_id or not title:
                continue
            locations = data.get("locations", [])
            primary = locations[0] if locations else {}
            location = ", ".join(
                str(primary.get(key, "")).strip()
                for key in ("city", "country")
                if primary.get(key)
            )
            city_slug = str(primary.get("city", "x")).replace(" ", "-")
            title_slug = title.replace(" ", "-")
            path = f"/job/{city_slug}-{title_slug}/{job_id}"
            jobs[job_id] = {
                "id": f"uniper_{job_id}",
                "title": title,
                "url": requests.compat.urljoin(f"{BASE_URL}/", path),
                "location": location,
                "posted": str(data.get("postingDate", ""))[:10],
            }
        page += 1

    if total is None or len(jobs) != total:
        raise RuntimeError(f"Uniper reported {total} jobs but parsed {len(jobs)}")
    return list(jobs.values())
