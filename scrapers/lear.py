"""Lear's public vacancies page: role headings followed by inline descriptions."""
import hashlib

from bs4 import BeautifulSoup

from ._http import make_session, fix_encoding
from .generic import HEADERS


def scrape(base_url: str) -> list[dict]:
    response = make_session().get(base_url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    fix_encoding(response)
    soup = BeautifulSoup(response.text, "html.parser")
    main = soup.select_one("div.main-page")
    if main is None:
        raise RuntimeError("Lear: vacancies content missing")
    # A heading paragraph contains only a bold title. Bold words within the
    # requirements prose (including protected-category restrictions) are not roles.
    headings = [p for p in main.find_all("p", recursive=False)
                if p.find("strong", recursive=False) is not None
                and p.get_text(" ", strip=True) == p.strong.get_text(" ", strip=True)]
    jobs = []
    for heading in headings:
        title = heading.get_text(" ", strip=True)
        parts = [title]
        for node in heading.next_siblings:
            if any(node is h for h in headings):
                break
            text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node).strip()
            if text:
                parts.append(text)
        body = "\n".join(parts)
        if len(body) < 200:
            raise RuntimeError(f"Lear: no substantive description for {title}")
        uid = hashlib.md5(f"lear_{title}".encode()).hexdigest()[:16]
        jobs.append({"id": f"lear_{uid}", "title": title, "url": base_url,
                     "location": "", "posted": "", "description": body})
    if not jobs:
        raise RuntimeError("Lear: no vacancies parsed")
    return jobs
