"""D. E. Shaw job parser for its server-rendered careers page."""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

URL = "https://www.deshaw.com/careers"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
JOB_PATH = re.compile(r"^/careers/(?!internships$|faq$|interviewing$|benefits$)[a-z0-9-]+-(\d+)$")


def scrape() -> list[dict]:
    response = make_session().get(URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    fix_encoding(response)
    soup = BeautifulSoup(response.text, "html.parser")

    jobs = []
    seen = set()
    for link in soup.select('a[href^="/careers/"]'):
        href = link.get("href", "")
        match = JOB_PATH.match(href)
        if not match or href in seen:
            continue
        seen.add(href)

        text = " ".join(link.get_text(" ", strip=True).split())
        if text.lower().startswith("icon "):
            text = text[5:]
        title = text.split(":", 1)[0].strip()
        if not title:
            continue

        jobs.append({
            "id": f"deshaw_{match.group(1)}",
            "title": title,
            "url": urljoin(URL, href),
            "location": "",
            "posted": "",
        })
    if not jobs:
        # The careers page fetched but no job anchors matched — the server-side
        # layout changed or the list moved behind JS. Returning [] would delist
        # D. E. Shaw; a firm this size always has openings, so raise.
        raise RuntimeError("deshaw: no jobs parsed from careers page")
    return jobs
