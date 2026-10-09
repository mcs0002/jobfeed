"""Workable public careers API scraper."""
from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(account: str, widget_account: str = "") -> list[dict]:
    # `widget_account` is a second Workable account polled via the v1 widget
    # API (numeric-id accounts have no v3 slug — e.g. ArrowResources 521391,
    # whose careers page embeds whr_embed(521391) while the legacy slug board
    # lives on under the old firm name). Results are merged, deduped by
    # shortcode.
    base_url = f"https://apply.workable.com/{account}"
    response = make_session().post(
        f"https://apply.workable.com/api/v3/accounts/{account}/jobs",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={
            "query": "",
            "location": [],
            "department": [],
            "worktype": [],
            "remote": [],
        },
        timeout=20,
    )
    response.raise_for_status()

    jobs = []
    for job in response.json().get("results", []):
        shortcode = job.get("shortcode") or job.get("id")
        if not shortcode:
            continue
        location_data = job.get("location") or {}
        if isinstance(location_data, dict):
            location = ", ".join(
                value for value in (
                    location_data.get("city", ""),
                    location_data.get("country", ""),
                ) if value
            )
        else:
            location = str(location_data)
        jobs.append({
            "id": f"workable_{shortcode}",
            "title": job.get("title", ""),
            "url": job.get("url") or f"{base_url}/j/{shortcode}/",
            "location": location,
            "posted": (job.get("created_at") or job.get("published_on") or "")[:10],
        })

    if widget_account:
        r = make_session().get(
            f"https://apply.workable.com/api/v1/widget/accounts/{widget_account}",
            params={"details": "true"}, headers=HEADERS, timeout=20,
        )
        r.raise_for_status()
        seen = {j["id"] for j in jobs}
        for job in r.json().get("jobs", []):
            shortcode = job.get("shortcode") or job.get("code") or job.get("id")
            if not shortcode or f"workable_{shortcode}" in seen:
                continue
            location = ", ".join(
                v for v in (job.get("city", ""), job.get("country", "")) if v)
            jobs.append({
                "id": f"workable_{shortcode}",
                "title": job.get("title", ""),
                "url": job.get("url") or job.get("application_url", ""),
                "location": location,
                "posted": (job.get("published_on") or job.get("created_at") or "")[:10],
            })
    return jobs
