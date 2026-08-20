"""Pinpoint public careers RSS scraper."""
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
CONTENT_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"


def scrape(feed_url: str) -> list[dict]:
    response = make_session().get(feed_url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise RuntimeError(f"pinpoint: unparseable RSS from {feed_url}: {exc}")

    channel = root.find("./channel")
    if channel is None:
        # Not a well-formed RSS document (no <channel>) — the endpoint is
        # serving a shell/error page, not a trustworthy feed. Fail loud.
        raise RuntimeError(f"pinpoint: no RSS channel in {feed_url}")

    jobs = []
    for item in root.findall("./channel/item"):
        url = (item.findtext("link") or "").strip()
        job_id = url.rstrip("/").rsplit("/", 1)[-1]
        if not url or not job_id:
            continue

        content = item.findtext(CONTENT_TAG) or ""
        soup = BeautifulSoup(content, "html.parser")
        location = ""
        for paragraph in soup.find_all("p"):
            text = paragraph.get_text(" ", strip=True)
            if text.lower().startswith("location:"):
                location = text.split(":", 1)[1].strip()
                break

        pub_date = (item.findtext("pubDate") or "").strip()
        try:
            posted = parsedate_to_datetime(pub_date).date().isoformat()
        except (TypeError, ValueError):
            posted = ""
        jobs.append({
            "id": f"pinpoint_{job_id}",
            "title": (item.findtext("title") or "").strip(),
            "url": url,
            "location": location,
            "posted": posted,
        })
    # A well-formed RSS <channel> with zero <item> elements is a TRUSTWORTHY
    # empty board — Pinpoint serves a valid feed with no items when a firm has
    # no openings. (Contrast the guards above: a missing/unparseable channel is
    # a shell/error and still raises.) Returning [] here lets the delister
    # correctly retire stale rows without purging on a false "board moved".
    return jobs
