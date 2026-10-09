"""Deutsche Börse Group official sitemap and JobPosting scraper."""
import concurrent.futures
import functools
import json
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

SITEMAP_URL = "https://careers.deutsche-boerse.com/sitemap.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def _get(session, url: str) -> requests.Response:
    for attempt in range(3):
        try:
            return session.get(url, headers=HEADERS, timeout=30)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise


def _fetch_job(session, url: str) -> tuple[dict | None, bool]:
    response = _get(session, url)
    if response.status_code == 404:
        return None, True
    response.raise_for_status()
    fix_encoding(response)
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        if data.get("@type") != "JobPosting":
            continue
        identifier = data.get("identifier") or {}
        job_id = identifier.get("value")
        if not job_id:
            continue
        locations = []
        for job_location in data.get("jobLocation") or []:
            address = job_location.get("address") or {}
            locality = address.get("addressLocality", "")
            country = address.get("addressCountry", "")
            location = ", ".join(part for part in (locality, country) if part)
            if location:
                locations.append(location)
        return {
            "id": f"deutscheboerse_{job_id}",
            "title": data.get("title", ""),
            "url": data.get("url") or url,
            "location": "; ".join(dict.fromkeys(locations)),
            "posted": data.get("datePosted", ""),
        }, False
    return None, False


def scrape() -> list[dict]:
    session = make_session()
    response = _get(session, SITEMAP_URL)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    urls = [
        element.text
        for element in root.iter()
        if element.tag.endswith("loc")
        and element.text
        and "/offer/" in element.text
    ]

    jobs = {}
    removed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for job, is_removed in executor.map(
            functools.partial(_fetch_job, session), urls
        ):
            removed += is_removed
            if job:
                jobs[job["id"]] = job
    if len(jobs) + removed != len(urls):
        raise RuntimeError(
            f"Deutsche Börse sitemap exposed {len(urls)} offers but "
            f"{len(jobs)} JobPosting records were parsed and "
            f"{removed} withdrawn offers returned 404"
        )
    return list(jobs.values())
