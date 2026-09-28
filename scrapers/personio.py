"""Personio hosted job-board scraper (public XML feed).

Every Personio careers site ({slug}.jobs.personio.de) exposes its postings as
one XML document at ``{base_url}/xml``: ``<workzag-jobs><position>...`` with
``id``, ``name``, ``office``, ``subcompany`` and the full description split
into named ``jobDescription`` sections. No auth, no pagination. Job URL is
``{base_url}/job/{id}``.
"""
import xml.etree.ElementTree as ET
from html import unescape

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "base_url": "https://flossbach-von-storch-ag.jobs.personio.de",
        "tenant": "fvs",   # optional label for job IDs
    }
    """
    base_url = config["base_url"].rstrip("/")
    tenant = config.get("tenant", base_url.split("//")[-1].split(".")[0])

    response = make_session().get(f"{base_url}/xml", headers=HEADERS, timeout=40)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    if root.tag != "workzag-jobs":
        raise RuntimeError(
            f"personio: unexpected feed root <{root.tag}> from {base_url}/xml"
        )

    jobs = {}
    for position in root.iter("position"):
        job_id = (position.findtext("id") or "").strip()
        title = unescape((position.findtext("name") or "").strip())
        if not job_id or not title:
            continue
        sections = []
        for desc in position.iter("jobDescription"):
            heading = (desc.findtext("name") or "").strip()
            value = (desc.findtext("value") or "").strip()
            if value:
                sections.append(f"<h3>{heading}</h3>\n{value}" if heading else value)
        jobs[job_id] = {
            "id": f"personio_{tenant}_{job_id}",
            "title": title,
            "url": f"{base_url}/job/{job_id}",
            "location": (position.findtext("office") or "").strip(),
            "description": "\n".join(sections),
            "posted": (position.findtext("createdAt") or "").strip()[:10],
        }
    return list(jobs.values())
