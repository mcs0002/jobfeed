"""
Lever public API scraper.
Endpoint: https://api.lever.co/v0/postings/{slug}?mode=json
No auth required — Lever publishes job postings as a public API.

Some tenants live on Lever's EU region (api.eu.lever.co) instead of the global
host; pass `api_base` to override (e.g. SEB).
"""
from ._http import make_session

DEFAULT_API_BASE = "https://api.lever.co"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(slug: str, api_base: str = DEFAULT_API_BASE) -> list[dict]:
    url = f"{api_base.rstrip('/')}/v0/postings/{slug}"
    jobs = []
    skip = 0
    limit = 100
    session = make_session()

    while True:
        r = session.get(
            url,
            params={"mode": "json", "limit": limit, "skip": skip},
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break

        for j in data:
            location = j.get("categories", {}).get("location", "")
            # Lever's public postings API includes descriptionPlain (text) and
            # additionalPlain (e.g. EEO blurbs). Concatenate so the stored
            # description matches what a candidate would read.
            desc_parts = [j.get("descriptionPlain", ""), j.get("additionalPlain", "")]
            description = "\n\n".join(p for p in desc_parts if p)
            jobs.append({
                "id": f"lv_{j['id']}",
                "title": j.get("text", ""),
                "url": j.get("hostedUrl", ""),
                "location": location,
                "posted": "",
                "description": description,
            })

        if len(data) < limit:
            break
        skip += limit

    return jobs
