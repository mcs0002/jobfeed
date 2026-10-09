"""Nürnberger Versicherung — public Elasticsearch job index.

The careers search on nuernberger.com is backed by an unauthenticated
Elasticsearch endpoint (`/search/prod-nv-com-nv-jobs/_search`). Each hit is a
page document; the actual posting (title, jobId, location/department lists,
tasks/qualifications/benefits text) lives in ``_source.content[0]``. Docs
without a numeric-id ``live_url`` are index/overview pages, not postings.
Descriptions are inline (SCRAPER strategy).
"""
import re

from ._http import make_session

SEARCH_URL = ("https://www.nuernberger.com/search/"
              "prod-nv-com-nv-jobs/_search?size=500")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
_JOB_URL_RE = re.compile(r"-\d+/?$")


def _names(value) -> str:
    if isinstance(value, list):
        return ", ".join(v.get("name", "") for v in value if isinstance(v, dict))
    return ""


def scrape(config: dict | None = None) -> list[dict]:
    session = make_session()
    resp = session.get(SEARCH_URL, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    hits = resp.json().get("hits", {}).get("hits", [])
    if not hits:
        raise RuntimeError("nuernberger: ES returned no hits (index moved?)")

    jobs = {}
    for hit in hits:
        src = hit.get("_source", {})
        url = src.get("live_url", "")
        if not _JOB_URL_RE.search(url):
            continue  # overview/index page doc, not a posting
        content = src.get("content") or []
        item = content[0] if isinstance(content, list) and content else {}
        title = (item.get("title") or "").strip()
        job_id = str(item.get("jobId") or "").strip()
        if not title or not job_id:
            continue
        body = " ".join(
            str(item.get(k) or "").strip()
            for k in ("description", "contentTasks", "contentQualifications",
                      "contentBenefits")
        ).strip()
        jobs[job_id] = {
            "id": f"nuernberger_{job_id}",
            "title": title,
            "url": url,
            "location": _names(item.get("location")),
            "description": body,
            "posted": "",
        }
    if not jobs:
        raise RuntimeError(
            "nuernberger: hits present but no postings parsed (shape drift?)")
    return list(jobs.values())
