import math
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ._http import make_session


HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
}


def _fetch_page(session, config: dict, page: int, page_size: int) -> dict:
    base_url = config["base_url"].rstrip("/")
    facet_filters = config.get("facet_filters", [])
    params = {
        "CurrentPage": page,
        "RecordsPerPage": page_size,
        "Distance": 50,
        "SearchType": 5,
        "Keyword": config.get("keyword", ""),
        "SearchResultsModuleName": config.get(
            "results_module", "Search Results"
        ),
        "SearchFiltersModuleName": config.get(
            "filters_module", ""
        ),
        "SortCriteria": 1,
        "SortDirection": 0,
        "IsPagination": "True",
    }
    for index, facet in enumerate(facet_filters):
        for key, value in facet.items():
            params[f"FacetFilters[{index}].{key}"] = value

    response = session.get(
        f"{base_url.rstrip('/')}/search-jobs/results",
        params=params,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _parse_results(
    config: dict, html: str
) -> tuple[list[dict], int]:
    base_url = config["base_url"].rstrip("/")
    soup = BeautifulSoup(html, "html.parser")
    results = soup.select_one("#search-results")
    if results is None or results.get("data-total-job-results") is None:
        # Some tenants (e.g. Cargill) put the count on a different element.
        results = soup.select_one("[data-total-job-results]")
    total = int(results.get("data-total-job-results", 0)) if results else 0
    jobs = []
    for item in soup.select(config.get("item_selector", ".sr-job-item")):
        link = item.select_one(
            config.get("link_selector", "a.sr-job-item__link")
        )
        if not link:
            continue
        job_id = str(link.get("data-job-id", "")).strip()
        title_selector = config.get("title_selector")
        title_element = item.select_one(title_selector) if title_selector else None
        title = (
            title_element.get_text(" ", strip=True)
            if title_element
            else link.get_text(" ", strip=True)
        )
        if not job_id or not title:
            continue
        location = item.select_one(
            config.get("location_selector", ".sr-job-location")
        )
        jobs.append({
            "id": f"talentbrew_{job_id}",
            "title": title,
            "url": urljoin(base_url, link.get("href", "")),
            "location": location.get_text(" ", strip=True) if location else "",
            "posted": "",
        })
    return jobs, total


def _scrape_facet_set(session, config: dict, facet_filters: list, jobs: dict) -> None:
    """Scrape one facet set into the shared jobs dict (dedup by id).

    TalentBrew ANDs multiple FacetFilters, so a single set must describe one
    division (e.g. Markets [L2]). To pull several divisions we call this once
    per set and union the results — see scrape().
    """
    page_size = int(config.get("page_size", 100))
    set_config = {**config, "facet_filters": facet_filters}
    first = _fetch_page(session, set_config, 1, page_size)
    first_jobs, total = _parse_results(set_config, first.get("results", ""))
    page_count = max(1, math.ceil(total / page_size))
    seen = {job["id"]: job for job in first_jobs}

    for page in range(2, page_count + 1):
        payload = _fetch_page(session, set_config, page, page_size)
        page_jobs, _ = _parse_results(set_config, payload.get("results", ""))
        for job in page_jobs:
            seen[job["id"]] = job
    if len(seen) != total:
        if len(seen) < 0.9 * total:
            raise RuntimeError(
                f"TalentBrew reported {total} jobs but parsed {len(seen)}"
            )
        print(
            f"WARN TalentBrew reported {total} jobs but parsed {len(seen)}; "
            "keeping partial results",
            file=sys.stderr,
        )
    jobs.update(seen)


def scrape(config: dict) -> list[dict]:
    # A board exposes its divisions as facet sets. `facet_filter_sets` is a
    # list of sets, each scraped independently and unioned (Citi: Markets +
    # IBD). `facet_filters` (a single set) stays supported for one-division
    # boards (Cargill TRADING). Each set is positively scoped; the always-on
    # negative filter still sheds any noise inside the division.
    facet_sets = config.get("facet_filter_sets")
    if facet_sets is None:
        facet_sets = [config.get("facet_filters", [])]

    jobs: dict = {}
    session = make_session()
    for facet_filters in facet_sets:
        _scrape_facet_set(session, config, facet_filters, jobs)
    return list(jobs.values())
