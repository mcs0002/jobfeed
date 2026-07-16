"""Haufe Umantis (Talent Management) hosted job-board scraper.

Umantis recruiting hubs ({co}.umantis.com or recruitingapp-{n}.de.umantis.com)
server-render the full vacancy list on ``/Jobs/All``: each posting is an
anchor ``a.HSTableLinkSubTitle`` with href ``/Vacancies/{id}/Description/{n}``
and the title as link text. One page, no tokens, plain requests.
"""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "base_url": "https://recruitingapp-5064.de.umantis.com",
        "tenant": "quoniam",   # optional label for job IDs
    }
    """
    base_url = config["base_url"].rstrip("/")
    tenant = config.get("tenant", base_url.split("//")[-1].split(".")[0])

    response = make_session().get(
        f"{base_url}/Jobs/All", headers=HEADERS, timeout=40
    )
    response.raise_for_status()
    fix_encoding(response)
    soup = BeautifulSoup(response.text, "html.parser")

    jobs = {}
    for link in soup.select("a.HSTableLinkSubTitle"):
        href = link.get("href", "")
        match = re.search(r"/Vacancies/(\d+)/Description/", href)
        title = link.get_text(" ", strip=True)
        if not match or not title:
            continue
        job_id = match.group(1)
        jobs[job_id] = {
            "id": f"umantis_{tenant}_{job_id}",
            "title": title,
            "url": urljoin(response.url, href),
            "location": "",
            "posted": "",
        }
    if not jobs and "HSTableLinkSubTitle" not in response.text:
        # An empty board still ships the table markup; a missing class name
        # means the layout changed or we got an interstitial — fail loud.
        raise RuntimeError(
            f"umantis: no vacancy anchors and no board markup at "
            f"{base_url}/Jobs/All (layout change or challenge page?)"
        )
    return list(jobs.values())
