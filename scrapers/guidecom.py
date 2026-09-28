"""GuideCom job portal scraper.

GuideCom is a German recruiting suite used by Helaba and several Sparkassen.
The list page at /jobportal/{tenant}/viewAusschreibungen.html renders every
opening server-side — no JS, no auth, no captcha. The page is a flat list of
Bootstrap cards; each card carries the title in `h5.card-title`, the location
in a `<span>` inside `.card-text`, and a `<a class="btn">` linking to the
detail page (whose URL stem is the canonical job ID).
"""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
BASE = "https://connect.guidecom.de/jobportal"
_JOB_HREF_RE = re.compile(r"viewAusschreibung/([^/]+)\.html")


def _location(card) -> str:
    text_p = card.select_one(".card-text")
    if not text_p:
        return ""
    # Drop the trailing "mehr" link and hidden filter spans, keep the
    # visible location text.
    parts = []
    for el in text_p.find_all(["span"], recursive=True):
        style = (el.get("style") or "").replace(" ", "")
        if "display:none" in style:
            continue
        txt = el.get_text(" ", strip=True)
        if txt:
            parts.append(txt)
    return ", ".join(parts) if parts else text_p.get_text(" ", strip=True).replace("mehr", "").strip()


def scrape(tenant: str) -> list[dict]:
    list_url = f"{BASE}/{tenant}/viewAusschreibungen.html"
    r = make_session().get(list_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    fix_encoding(r)
    soup = BeautifulSoup(r.text, "html.parser")
    jobs: dict[str, dict] = {}

    for card in soup.select(".card-body"):
        link = card.select_one(f"a[href*='viewAusschreibung/']")
        if link is None:
            continue
        m = _JOB_HREF_RE.search(link.get("href", ""))
        if not m:
            continue
        job_id = m.group(1)

        title_el = card.select_one(".card-title")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            continue

        jobs[job_id] = {
            "id": f"gc_{tenant}_{job_id}",
            "title": title,
            "url": urljoin(list_url, link["href"]),
            "location": _location(card),
            "posted": "",
        }

    if not jobs:
        # Fetched the portal but no viewAusschreibung cards parsed — the
        # server-side layout moved. Returning [] would delist the tenant; raise.
        raise RuntimeError(f"guidecom: no jobs parsed for tenant {tenant}")
    return list(jobs.values())
