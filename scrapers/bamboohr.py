"""BambooHR hosted careers scraper (public JSON, no auth).

Every BambooHR careers site ({slug}.bamboohr.com) exposes its open roles as
JSON at ``/careers/list`` — ``{"meta":{"totalCount":N},"result":[...]}`` — with
``id``, ``jobOpeningName``, ``employmentStatusLabel`` and a ``location`` city/
state. The listing carries no body, so we fetch ``/careers/{id}/detail`` per job
for the ``description`` (and a fuller location incl. country) — BambooHR boards
are small (boutique/impact managers), so one GET per job is cheap.

Job page for a human: ``https://{slug}.bamboohr.com/careers/{id}``.
"""
from ._http import make_session

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Accept": "application/json",
}


def _location(loc: dict) -> str:
    if not isinstance(loc, dict):
        return ""
    parts = [loc.get("city"), loc.get("state"), loc.get("addressCountry")]
    return ", ".join(p for p in parts if p)


def scrape(config: dict) -> list[dict]:
    """
    config = {"slug": "blueorchard"}   # -> blueorchard.bamboohr.com
    """
    slug = config["slug"]
    base = f"https://{slug}.bamboohr.com/careers"
    session = make_session()

    resp = session.get(f"{base}/list", headers=HEADERS, timeout=40)
    resp.raise_for_status()
    payload = resp.json()
    if "result" not in payload:
        raise RuntimeError(f"bamboohr: no 'result' in {base}/list payload")

    jobs = {}
    for item in payload["result"]:
        job_id = str(item.get("id") or "").strip()
        title = (item.get("jobOpeningName") or "").strip()
        if not job_id or not title:
            continue

        description = ""
        location = _location(item.get("location"))
        try:
            detail = session.get(f"{base}/{job_id}/detail", headers=HEADERS,
                                 timeout=30)
            detail.raise_for_status()
            opening = detail.json().get("result", {}).get("jobOpening", {})
            description = (opening.get("description") or "").strip()
            location = _location(opening.get("location")) or location
        except Exception:
            pass  # keep the listing location; enricher/nightly backstop can retry

        jobs[job_id] = {
            "id": f"bamboohr_{slug}_{job_id}",
            "title": title,
            "url": f"{base}/{job_id}",
            "location": location,
            "description": description,
            "posted": "",
        }
    return list(jobs.values())
