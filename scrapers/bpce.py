"""Groupe BPCE careers feed scraper.

The BPCE group careers portals (Natixis CIB, Banque Populaire, Caisse
d'Epargne, ...) are decoupled WordPress sites sharing one custom REST API:
``POST {base_url}/wp-json/bpce/v1/search/jobs`` with a JSON body
``{"from", "size", "lang"}``. The response is ``data.items[]`` with the full
``description`` HTML inline plus ``localisation`` and a relative ``link.url``.
Plain HTTP, no auth/WAF. ``base_url`` carries the ``/app`` suffix where
WordPress lives; the public job links hang off the bare origin.
"""
from urllib.parse import urlsplit

from ._http import assert_complete, make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}


def _abs_url(origin: str, link) -> str:
    if isinstance(link, dict):
        link = link.get("url", "")
    link = link or ""
    if link.startswith("http"):
        return link
    return f"{origin}{link}" if link else ""


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "base_url": "https://recrutement.natixis.com/app",  # WP root (/app)
        "lang": "en",
        "page_size": 500,
        "tenant": "natixis",   # optional label for job IDs
    }
    """
    base_url = config["base_url"].rstrip("/")
    lang = config.get("lang", "en")
    page_size = config.get("page_size", 500)
    parts = urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    tenant = config.get("tenant") or parts.netloc.split(".")[0]
    endpoint = f"{base_url}/wp-json/bpce/v1/search/jobs"

    session = make_session()
    jobs = []
    offset = 0
    total = None
    while True:
        r = session.post(
            endpoint,
            headers={**HEADERS, "Referer": f"{origin}/"},
            json={"from": offset, "size": page_size, "lang": lang},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json().get("data", {}) or {}
        items = data.get("items", []) or []
        if not items:
            break

        for it in items:
            job_id = it.get("post_id") or it.get("job_number") or it.get("technical_id")
            title = it.get("title", "")
            if not job_id or not title:
                continue
            jobs.append({
                "id": f"bpce_{tenant}_{job_id}",
                "title": title,
                "url": _abs_url(origin, it.get("link")),
                "location": it.get("localisation", ""),
                "description": it.get("description", ""),
                "posted": str(it.get("date", ""))[:10],
            })

        offset += page_size
        total = data.get("total", 0) or None
        if total is None or offset >= total:
            break

    assert_complete(len(jobs), total, f"BPCE/{tenant}")
    return jobs
