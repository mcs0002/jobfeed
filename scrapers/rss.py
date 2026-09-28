"""Generic RSS 2.0 job-feed scraper.

Some small self-hosted career sites (e.g. TYPO3 EXT:news) publish their openings
as a plain RSS feed: ``<channel><item><title/><link/><description/><pubDate/>``.
This adapter parses any such feed into job records. The ``<description>`` (often
an HTML teaser) is used as the body; fuller text, if needed, fills from the
server-rendered detail page via the generic enricher.
"""
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html import unescape

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def _posted(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return value[:10]


def scrape(config: dict) -> list[dict]:
    """config = {"feed_url": "...", "prefix": "lbbw"}"""
    feed_url = config["feed_url"]
    prefix = config.get("prefix") or feed_url.split("//")[-1].split(".")[0]

    resp = make_session().get(feed_url, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)

    jobs = {}
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        guid = (item.findtext("guid") or link).strip()
        jobs[guid] = {
            "id": f"rss_{prefix}_{guid}",
            "title": unescape(title),
            "url": link,
            "location": "",
            "description": unescape((item.findtext("description") or "").strip()),
            "posted": _posted(item.findtext("pubDate") or ""),
        }
    return list(jobs.values())
