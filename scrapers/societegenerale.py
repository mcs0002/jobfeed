"""Société Générale server-rendered global careers inventory."""
import re

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

URL = "https://careers.societegenerale.com/en/Technical/all-job-offers"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape() -> list[dict]:
    response = make_session().get(URL, headers=HEADERS, timeout=60)
    response.raise_for_status()
    fix_encoding(response)
    soup = BeautifulSoup(response.text, "html.parser")
    total_element = soup.select_one(".views-element-container strong")
    total_match = re.search(
        r"\d[\d,.]*", total_element.get_text(strip=True) if total_element else ""
    )
    total = int(re.sub(r"[,.]", "", total_match.group())) if total_match else 0

    jobs = {}
    for card in soup.select("[data-offer-id]"):
        link = card.select_one('a[href*="/job-offers/"]')
        job_id = str(card.get("data-offer-id", "")).strip()
        title = link.get_text(" ", strip=True) if link else ""
        href = link.get("href", "") if link else ""
        if not job_id or not title or not href:
            continue
        details = [
            item.get_text(" ", strip=True)
            for item in card.select(".tags .nobreak")
        ]
        jobs[job_id] = {
            "id": f"socgen_{job_id}",
            "title": title,
            "url": href,
            "location": details[0] if details else "",
            "posted": "",
        }

    if len(jobs) != total:
        raise RuntimeError(
            f"Société Générale reported {total} jobs but parsed {len(jobs)}"
        )
    return list(jobs.values())
