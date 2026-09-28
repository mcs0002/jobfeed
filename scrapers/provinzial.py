"""Provinzial group job feed (karriere-provinzial.de).

One static JSON at ``/results.json`` lists the whole group (~1.2k rows), of
which ~1k are ``type: "Agentur"`` self-employed agency posts. We keep only the
central ``Konzern`` rows (~190) — the slice where Provinzial Asset Management
and other head-office finance roles post. Detail pages are server-rendered
TYPO3 (HTTP description strategy).
"""
from ._http import make_session

FEED_URL = "https://karriere-provinzial.de/results.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
KEEP_TYPES = {"Konzern"}


def scrape(config: dict | None = None) -> list[dict]:
    keep = set((config or {}).get("types") or KEEP_TYPES)
    session = make_session()
    resp = session.get(FEED_URL, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    payload = resp.json()
    items = payload if isinstance(payload, list) else (
        payload.get("jobs") or payload.get("results"))
    if not items:
        raise RuntimeError("provinzial: results.json empty (feed moved?)")

    jobs = {}
    for item in items:
        if item.get("type") not in keep:
            continue
        job_id = str(item.get("uid") or "").strip()
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not job_id or not title or not url:
            continue
        jobs[job_id] = {
            "id": f"provinzial_{job_id}",
            "title": title,
            "url": url,
            "location": (item.get("city") or "").strip(),
            "posted": "",
        }
    if not jobs:
        raise RuntimeError(
            "provinzial: feed present but no Konzern rows parsed "
            "(type taxonomy drift?)")
    return list(jobs.values())
