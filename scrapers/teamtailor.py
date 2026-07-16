"""Teamtailor public career-site scraper."""
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session


HEADERS = {
    "Accept": "text/vnd.turbo-stream.html",
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
}
MAX_PAGES = 200


def _text(element) -> str:
    return " ".join(
        value.strip()
        for value in element.find_all(string=True)
        if value.strip()
    )


def scrape(base_url: str) -> list[dict]:
    base_url = base_url.rstrip("/")
    jobs = {}
    page = 1
    session = make_session()

    while True:
        response = session.get(
            f"{base_url}/jobs/show_more",
            params={"page": page},
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        fix_encoding(response)
        soup = BeautifulSoup(response.text, "html.parser")
        links = soup.select('a[href*="/jobs/"]:not([href*="/jobs/show_more"])')

        for link in links:
            href = urljoin(f"{base_url}/", link.get("href", ""))
            path = urlparse(href).path
            match = re.search(r"/jobs/(\d+)(?:-|$)", path)
            title = _text(link)
            if not match or not title:
                continue

            item = link.find_parent("li")
            details = item.select_one(".text-base") if item else None
            location = _text(details) if details else ""
            job_id = match.group(1)
            jobs[job_id] = {
                "id": f"teamtailor_{job_id}",
                "title": title,
                "url": href,
                "location": location,
                "posted": "",
            }

        next_page = soup.select_one(
            f'a[href*="/jobs/show_more?page={page + 1}"]'
        )
        if not links or not next_page or page >= MAX_PAGES:
            break
        page += 1

    if jobs:
        return list(jobs.values())

    # Zero anchors parsed from the HTML. The newer Teamtailor layout renders
    # the board client-side (no server-rendered /jobs/ anchors), so a zero-parse
    # is ambiguous: it could be a genuinely empty board OR a broken selector.
    # Disambiguate with the RSS feed, which every Teamtailor board exposes at
    # <base>/jobs.rss and which carries the real openings:
    #   * valid channel, 0 items   -> trusted-empty board -> return []
    #   * valid channel, N items   -> HTML path is stale; parse the RSS and
    #                                 return those jobs (recover rather than just
    #                                 raise — the board isn't actually empty)
    #   * unavailable/unparseable  -> can't confirm -> raise (as before)
    return _scrape_rss_fallback(session, base_url)


RSS_TITLE_ID_RE = re.compile(r"/jobs/(\d+)(?:-|$)")


def _scrape_rss_fallback(session, base_url: str) -> list[dict]:
    feed_url = f"{base_url}/jobs.rss"
    response = session.get(feed_url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise RuntimeError(
            f"teamtailor: HTML parsed 0 jobs and RSS at {feed_url} "
            f"is unparseable: {exc}"
        )

    channel = root.find("./channel")
    if channel is None:
        raise RuntimeError(
            f"teamtailor: HTML parsed 0 jobs and {feed_url} has no RSS channel"
        )

    jobs = {}
    for item in channel.findall("item"):
        url = (item.findtext("link") or "").strip()
        path = urlparse(url).path
        match = RSS_TITLE_ID_RE.search(path)
        title = (item.findtext("title") or "").strip()
        if not match or not title:
            continue
        job_id = match.group(1)
        jobs[job_id] = {
            "id": f"teamtailor_{job_id}",
            "title": title,
            "url": url,
            "location": "",
            "posted": "",
        }

    # Valid channel with zero usable items = genuinely empty board (trusted).
    return list(jobs.values())
