"""Teamio / jobs.cz (Alma Career / LMC) careersite widget scraper.

Branded careersites like ``{brand}.jobs.cz`` render their jobs client-side from a
GraphQL widget API: ``POST https://api.capybara.lmc.cz/api/graphql/widget`` with
an ``X-API-KEY`` header and a ``widgetId`` (a UUID identifying the careersite).
Both are public values embedded in the page's runtime JS (the widgetId isn't in
the static HTML, so it's captured once from the live request). Jobs come back in
a recursively-nested ``groupedJobAds`` tree with a ``paginator``. Body = the
``teaser`` (detail pages are an SPA). Job page: ``{host}/vacancy-detail?r=detail&id={id}``.
"""
from ._http import make_session

ENDPOINT = "https://api.capybara.lmc.cz/api/graphql/widget"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Safari/605.1.15"}

_QUERY = """query($widgetId: ID!, $host: String, $page: Int, $filters: [JobAdFilter!]!, $useExampleData: Boolean!) {
 widget(id: $widgetId, host: $host, useExampleData: $useExampleData) {
  jobAdList(page: $page, filters: $filters) {
   paginator { currentPage lastPage }
   groupedJobAds { ...G groups { ...G groups { ...G groups { ...G } } } }
  }
 }
}
fragment G on JobAdGroup { jobAds { id title validFrom teaser locations { city country } } }"""


def _collect(group: dict, out: list):
    for job in (group.get("jobAds") or []):
        out.append(job)
    for sub in (group.get("groups") or []):
        _collect(sub, out)


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "widget_id": "c47d3d4b-...",   # UUID of the careersite widget
        "host": "ceztrading.jobs.cz",
        "api_key": "13e8...",          # public widget key (re-capture if it rotates)
        "prefix": "cez",
    }
    """
    host = config["host"]
    prefix = config["prefix"]
    session = make_session()
    headers = {**HEADERS, "Content-Type": "application/json", "X-API-KEY": config["api_key"],
               "Origin": f"https://{host}", "Referer": f"https://{host}/"}

    jobs = {}
    page, last = 1, 1
    while page <= last:
        variables = {"widgetId": config["widget_id"], "host": host, "page": page,
                     "filters": [], "useExampleData": False}
        resp = session.post(ENDPOINT, json={"query": _QUERY, "variables": variables},
                            headers=headers, timeout=40)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"teamio[{prefix}]: {payload['errors'][:1]}")
        job_list = (payload.get("data") or {}).get("widget", {}).get("jobAdList") or {}
        last = (job_list.get("paginator") or {}).get("lastPage") or 1
        found = []
        _collect(job_list.get("groupedJobAds") or {}, found)
        for job in found:
            job_id = str(job.get("id") or "").strip()
            title = (job.get("title") or "").strip()
            if not job_id or not title or job_id in jobs:
                continue
            loc = (job.get("locations") or [{}])[0]
            location = ", ".join(p for p in (loc.get("city"), loc.get("country")) if p)
            jobs[job_id] = {
                "id": f"teamio_{prefix}_{job_id}",
                "title": title,
                "url": f"https://{host}/vacancy-detail?r=detail&id={job_id}",
                "location": location,
                "description": (job.get("teaser") or "").strip(),
                "posted": (job.get("validFrom") or "")[:10],
            }
        page += 1
    return list(jobs.values())
