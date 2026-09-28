"""Zoho Recruit career-site scraper — jobs from the page's inline hydration JSON.

Zoho Recruit career sites ({portal}.zohorecruit.{tld}/jobs/Careers) are SPAs,
but the initial HTML embeds the full published-jobs list as HTML-entity-encoded
JSON (flat objects: ``Posting_Title``, ``Job_Opening_Name``, ``City``,
``Country``, ``id``, ``Remote_Job``, ...). We unescape and extract those objects
directly — no auth, no CSRF digest, no browser. Detail pages are SPA-rendered so
no body is available here; the descriptive Zoho titles + location carry the
tagging signal. Job page: ``/jobs/Careers/{id}``.
"""
import json
import re
from html import unescape

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
_OBJ = re.compile(r'\{[^{}]*"Posting_Title"[^{}]*\}')


def scrape(config: dict) -> list[dict]:
    """config = {"portal": "freightinvestorservices", "tld": "eu"}"""
    portal = config["portal"]
    tld = config.get("tld", "eu")
    host = f"https://{portal}.zohorecruit.{tld}"

    resp = make_session().get(f"{host}/jobs/Careers", headers=HEADERS, timeout=40)
    resp.raise_for_status()
    text = unescape(resp.text)

    objs = _OBJ.findall(text)
    if not objs:
        raise RuntimeError(f"zoho_recruit: no job objects in {host}/jobs/Careers")

    jobs = {}
    for raw in objs:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        job_id = str(item.get("id") or "").strip()
        title = (item.get("Posting_Title") or item.get("Job_Opening_Name") or "").strip()
        if not job_id or not title:
            continue
        location = ", ".join(p for p in (item.get("City"), item.get("Country")) if p)
        jobs[job_id] = {
            "id": f"zoho_{portal}_{job_id}",
            "title": title,
            "url": f"{host}/jobs/Careers/{job_id}",
            "location": location,
            "description": "",
            "posted": "",
        }
    return list(jobs.values())
