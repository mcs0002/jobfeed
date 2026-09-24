"""Getnoticed careers platform scraper — public JSON list.

Getnoticed sites (e.g. karriere.abnamro.de / Hauck Aufhäuser Lampe) expose
paginated JSON at ``{base}/api/vacancy/`` — ``{"vacancies":[{id,slug,title,
city,...}], "meta":{"num_total_hits":N,"pageNumber":P,"maxPerPage":10,
"totalPageCount":P}}``. Job page: ``{base}/stellenangebote/{slug}``
(JS-rendered, so no body — descriptive title + city carry the signal; NONE
strategy).

The endpoint bot-filters a generic UA (404 to a plain ``requests`` UA,
confirmed 2026-07-09 — same signature as ``scrapers/abnamro.py``'s
werkenbijabnamro.nl endpoint, which this site now shares infra with post
merger); a real browser UA + Accept/Referer/X-Requested-With restores the 200.
Pagination IS reachable via HTTP despite an earlier note to the contrary —
``?pageNumber=N`` walks the full board (verified page 2 and 3 both return
distinct vacancies); this loops it to ``meta.totalPageCount``.
"""
from ._http import assert_complete, make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

MAX_PAGES = 40  # safety cap, mirrors scrapers/abnamro.py


def scrape(config: dict) -> list[dict]:
    """config = {"base": "https://karriere.abnamro.de", "prefix": "hal"}"""
    base = config["base"].rstrip("/")
    prefix = config["prefix"]
    headers = {**HEADERS, "Referer": f"{base}/stellenangebote"}
    session = make_session()

    jobs = {}
    page = 1
    total_pages = 1
    total = None
    while page <= total_pages and page <= MAX_PAGES:
        resp = session.get(
            f"{base}/api/vacancy/", headers=headers,
            params={"pageNumber": page}, timeout=40,
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("vacancies")
        if rows is None:
            raise RuntimeError(f"getnoticed: no 'vacancies' from {base}")

        meta = payload.get("meta") or {}
        total = meta.get("num_total_hits")
        total_pages = meta.get("totalPageCount") or 1

        if not rows:
            break
        for item in rows:
            job_id = str(item.get("id") or "").strip()
            title = (item.get("title") or "").strip()
            slug = (item.get("slug") or "").strip()
            if not job_id or not title:
                continue
            city = item.get("city") or ""
            jobs[job_id] = {
                "id": f"getnoticed_{prefix}_{job_id}",
                "title": title,
                "url": f"{base}/stellenangebote/{slug}" if slug else base,
                "location": city.split(",")[0].strip() if city else "",
                "description": "",
                "posted": (item.get("created") or "")[:10],
            }
        page += 1

    assert_complete(len(jobs), total, f"getnoticed[{prefix}]")
    return list(jobs.values())
