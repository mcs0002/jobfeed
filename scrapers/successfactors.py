"""SAP SuccessFactors Recruiting Marketing public search scraper."""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import assert_complete, fix_encoding, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}

# Some tenants (notably SMBC's regional boards) title every posting by its
# banking RANK ("Officer", "Vice President", …) and put the real role in the
# jobFacility/department field. A bare-rank title is useless to the tagger, so
# when the title is exactly one of these we fold the facility in.
_RANKS = {
    "officer", "analyst", "senior analyst", "associate", "senior associate",
    "assistant vice president", "vice president", "senior vice president",
    "director", "executive director", "managing director", "manager",
    "assistant manager", "deputy manager", "avp", "vp", "svp", "evp", "md",
}


def scrape(base_url: str, search_params: dict | None = None) -> list[dict]:
    base_url = base_url.rstrip("/")
    session = make_session()
    jobs = {}
    start = 0
    total = 0  # ensure defined even if the first page has no results

    while True:
        params = {
            "q": "",
            "sortColumn": "referencedate",
            "sortDirection": "desc",
            "startrow": start,
        }
        params.update(search_params or {})
        response = session.get(
            f"{base_url}/search/",
            params=params,
            headers=HEADERS,
            timeout=40,
        )
        response.raise_for_status()
        fix_encoding(response)
        soup = BeautifulSoup(response.text, "html.parser")
        links = soup.select("a.jobTitle-link")
        if not links:
            break

        page_ids = set()
        for link in links:
            href = link.get("href", "")
            match = re.search(r"/(\d+)/?$", href)
            if not href or not match:
                continue
            container = link.find_parent("tr") or link.find_parent("li", class_="job-tile")
            location_el = container.select_one(".jobLocation") if container else None
            facility_el = container.select_one(".jobFacility") if container else None
            job_id = match.group(1)
            page_ids.add(job_id)

            title = link.get_text(" ", strip=True)
            facility = facility_el.get_text(" ", strip=True) if facility_el else ""
            # Bare rank-only titles carry no role info — fold in the department.
            if facility and title.strip().lower() in _RANKS:
                title = f"{title} — {facility}"

            jobs[job_id] = {
                "id": f"sf_{job_id}",
                "title": title,
                "url": urljoin(f"{base_url}/", href),
                "location": location_el.get_text(" ", strip=True) if location_el else "",
                "posted": "",
            }

        pagination_el = soup.select_one(".paginationLabel")
        page_text = (
            pagination_el.get_text(" ", strip=True)
            if pagination_el else soup.get_text(" ", strip=True)
        )
        total_match = re.search(
            r"(?:of|von|di|de)\s+(\d[\d,.]*)(?:\s+(?:jobs|positions|Stellen))?",
            page_text,
            re.IGNORECASE,
        )
        if not total_match:
            total_match = re.search(
                r"Showing\s+(\d[\d,.]*)\s+Jobs?\b",
                page_text,
                re.IGNORECASE,
            )
        total = (
            int(re.sub(r"[,.]", "", total_match.group(1)))
            if total_match else len(jobs)
        )
        if not page_ids:
            break
        start += len(page_ids)
        if start >= total:
            break

    # Fleet-consistent completeness gate: raise below the 0.9 band, accept
    # silently within it (a small shortfall is dedup/rounding at page
    # boundaries, not data loss). Replaces the old stderr WARN, which fired
    # inside the accepted band and diverged from the shared assert_complete.
    assert_complete(len(jobs), total, "SuccessFactors")
    return list(jobs.values())
