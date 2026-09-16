"""Fetch Société Générale descriptions from the structured visible page.

The page's ``JobPosting.description`` JSON-LD concatenates headings, paragraphs,
and list items (``ResponsibilitiesThis ... contract.The ...``).  The visible DOM
retains that structure in three stable job-detail sections, so use those instead
of the generic JSON-LD-first extractor.
"""
import re

import requests
from bs4 import BeautifulSoup

from .descriptions import HEADERS


_URL_RE = re.compile(
    r"^https?://careers\.societegenerale\.com/(?:[^/]+/)?job-offers/",
    re.IGNORECASE,
)
_SECTION_IDS = (
    "job-detail-description",
    "job-detail-profile",
    "job-detail-group",
    "job-detail-diversite",
)
_MIN_DESCRIPTION_CHARS = 300


def is_societegenerale(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def extract_description(page_html: str) -> str:
    """Extract the visible description sections while preserving block breaks."""
    soup = BeautifulSoup(page_html or "", "html.parser")
    blocks: list[str] = []
    for section_id in _SECTION_IDS:
        node = soup.find(id=section_id)
        if not node:
            continue
        list_items: list[str] = []

        def flush_list() -> None:
            if list_items:
                blocks.append("\n".join(f"• {item}" for item in list_items))
                list_items.clear()

        for element in node.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
            # A paragraph nested inside a list item is represented by the item.
            if element.name == "p" and element.find_parent("li"):
                continue
            text = " ".join(element.get_text(" ", strip=True).split())
            if not text:
                continue
            if element.name == "li":
                list_items.append(text)
            else:
                flush_list()
                blocks.append(text)
        flush_list()
    description = "\n\n".join(blocks).strip()
    return description if len(description) >= _MIN_DESCRIPTION_CHARS else ""


def description(url: str, session: requests.Session | None = None,
                timeout: int = 15) -> str:
    """Return one structured SG job description, or ``""`` on failure."""
    if not is_societegenerale(url):
        return ""
    getter = session or requests
    try:
        response = getter.get(
            url,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    return extract_description(response.text)
