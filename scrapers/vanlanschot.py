"""Van Lanschot Kempen careers scraper (bespoke Sitecore/Next.js JSON list).

The careers site exposes its full vacancy list as JSON at
``/api/vlk/vacancies/get?sc_site=careers&sc_lang={lang}`` — one payload with
``vacancies`` (id, reference, title, location, relative ``url``, isInternship,
expertise). The list carries no body; each vacancy page
(``/en-nl/vacancies/{slug}``) IS server-rendered, so the description is filled
by the generic server-rendered enricher (coverage strategy = HTTP), not here.
"""
from ._http import make_session

BASE = "https://careers.vanlanschotkempen.com"
API = BASE + "/api/vlk/vacancies/get"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Accept": "application/json",
}


def scrape(config: dict | None = None) -> list[dict]:
    """config = {"lang": "en-NL"}  (default en-NL)"""
    config = config or {}
    lang = config.get("lang", "en-NL")

    resp = make_session().get(
        API, params={"sc_site": "careers", "sc_lang": lang},
        headers=HEADERS, timeout=40)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("vacancies")
    if not isinstance(rows, list):
        raise RuntimeError("vanlanschot: no 'vacancies' list in payload")

    jobs = {}
    for item in rows:
        ref = str(item.get("reference") or item.get("id") or "").strip()
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not ref or not title or not url:
            continue
        jobs[ref] = {
            "id": f"vlk_{ref}",
            "title": title,
            "url": url if url.startswith("http") else BASE + url,
            "location": (item.get("location") or "").strip(),
            "description": "",
            "posted": "",
        }
    return list(jobs.values())
