"""Wells Fargo official sitemap job scraper."""
import re
import xml.etree.ElementTree as ET

from ._http import make_session
from vocab import FRONT_OFFICE_KEYWORDS

SITEMAP_URL = "https://www.wellsfargojobs.com/sitemap.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# The public sitemap is the whole bank (~1900 roles, mostly retail). Wells
# Fargo *Securities* is the markets arm we actually want, so by default we keep
# only roles whose title-slug carries a front-office tell (vocab.py). Pass
# keywords=[] to disable scoping and pull the full board.


def scrape(keywords=FRONT_OFFICE_KEYWORDS) -> list[dict]:
    response = make_session().get(SITEMAP_URL, headers=HEADERS, timeout=40)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    jobs = {}
    for node in root.findall("s:url", NS):
        location_node = node.find("s:loc", NS)
        if location_node is None or not location_node.text:
            continue
        url = location_node.text
        match = re.search(r"/en/jobs/r-(\d+)/([^/]+)/?$", url)
        if not match:
            continue
        job_id, slug = match.groups()
        if keywords and not any(k in slug for k in keywords):
            continue
        lastmod_node = node.find("s:lastmod", NS)
        jobs[job_id] = {
            "id": f"wellsfargo_{job_id}",
            "title": slug.replace("-", " ").title(),
            "url": url,
            "location": "",
            "posted": lastmod_node.text if lastmod_node is not None else "",
        }

    return list(jobs.values())
