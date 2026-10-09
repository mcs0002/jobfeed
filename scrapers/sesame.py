"""Sesame HR public vacancies scraper.

Endpoint: https://{host}.sesametime.com/api/v3/companies/{slug}/public-vacancies
No auth. The slug is the one in the public board URL
(``app.sesametime.com/jobs/<slug>/all``). The API host is per tenant region:
the same request against the wrong host answers ``200`` with ``total: 0``
rather than an error, so ``host`` must be configured, never guessed. Read it
from the board page's own XHR (``back-eu2`` for GP Bullhound).

The listing payload carries the full HTML description, so no per-job fetch.
It also carries internal fields (candidate counts, recruiter contacts), which
are deliberately not copied.
"""
import html

from ._http import make_session
from .enrich.descriptions import _extract_text

API = "https://{host}.sesametime.com/api/v3/companies/{slug}/public-vacancies"
BOARD = "https://app.sesametime.com/jobs/{slug}/{id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Accept": "application/json",
}
PAGE_SIZE = 100
MAX_PAGES = 20


def scrape(config: dict) -> list[dict]:
    slug, host = config["slug"], config["host"]
    session = make_session()
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        r = session.get(API.format(host=host, slug=slug), headers=HEADERS,
                        params={"limit": PAGE_SIZE, "page": page}, timeout=30)
        r.raise_for_status()
        payload = r.json()
        for v in payload.get("data", []):
            if v.get("status") != "open" or not v.get("public", True):
                continue
            desc = v.get("description") or ""
            jobs.append({
                "id": f"sesame_{v['id']}",
                "title": (v.get("name") or "").strip(),
                "url": BOARD.format(slug=slug, id=v["id"]),
                "location": ", ".join(
                    x for x in (v.get("addressCity"), v.get("addressCountry")) if x),
                "posted": (v.get("openedAt") or v.get("createdAt") or "")[:10],
                "description": _extract_text(html.unescape(desc)) if desc else "",
            })
        meta = payload.get("meta") or {}
        if page >= int(meta.get("lastPage") or 1):
            break
    return jobs
