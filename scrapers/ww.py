"""W&W (Wüstenrot & Württembergische) in-house job API.

The ww-ag.com career board is a Vue app over ``/api/jobs/v2/{portal}/list``
(portal id 1119260; params documented in the site's joblist-config-json.json).
The full board is ~1.2k rows, ~1k of which are self-employed agent posts
(Wüstenrot Immobilien / Bausparkasse), so we scope server-side with the
``companies`` filter — W&W Asset Management GmbH plus the group's central
entities. ``content`` carries the job body inline (SCRAPER strategy).

Note: career2.successfactors.eu?company=wwinformat is only W&W Informatik
(the IT subsidiary) — not this board.
"""
from ._http import make_session

BASE = "https://www.ww-ag.com"
LIST_URL = BASE + "/api/jobs/v2/1119260/list"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
           "Accept": "application/json"}
DEFAULT_COMPANIES = (
    "W&W Asset Management GmbH",
    "Wüstenrot & Württembergische AG",
    "W&W Service GmbH",
    "Württembergische Lebensversicherung AG",
    "Württembergische Versicherung AG",
)
PAGE_SIZE = 100
MAX_PAGES = 30


def scrape(config: dict | None = None) -> list[dict]:
    companies = (config or {}).get("companies") or list(DEFAULT_COMPANIES)
    session = make_session()
    jobs: dict = {}
    for company in companies:
        page = 1
        fetched = 0
        total = None
        while page <= MAX_PAGES:
            resp = session.get(
                LIST_URL,
                params={"_page": page, "_limit": PAGE_SIZE,
                        "companies": company},
                headers=HEADERS, timeout=40)
            resp.raise_for_status()
            if total is None:
                total = int(resp.headers.get("x-total-count", 0))
            items = resp.json()
            if not isinstance(items, list):
                raise RuntimeError("ww: list endpoint did not return a list")
            if not items:
                break
            for item in items:
                job_id = str(item.get("id") or "").strip()
                title = (item.get("title") or "").strip()
                if not job_id or not title:
                    continue
                fetched += 1
                link = item.get("link") or ""
                jobs[job_id] = {
                    "id": f"ww_{job_id}",
                    "title": title,
                    "url": BASE + link if link.startswith("/") else link,
                    "location": (item.get("location") or "").strip(),
                    "description": (item.get("content") or "").strip(),
                    "posted": "",
                }
            if fetched >= total:
                break
            page += 1
        if total and fetched < total:
            raise RuntimeError(
                f"ww: company '{company}' reported {total} jobs but "
                f"fetched {fetched}")
    return list(jobs.values())
