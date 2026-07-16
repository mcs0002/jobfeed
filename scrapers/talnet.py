"""WCN/TAL.net server-rendered job board scraper."""
import re

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(board_url: str, fetch_detail: bool = True) -> list[dict]:
    """Scrape a tal.net/WCN board.

    ``fetch_detail`` controls the classic table layout's per-job detail GET
    (used only to read the location). Set it False for tenants behind **Oleeo
    Protect** — its anti-bot trips on request volume (listing + one GET per job
    = ~N+1 sequential requests), and once tripped the IP is ALTCHA-gated for
    *every* tal.net tenant on that scan. Some boards (e.g. Evercore) don't even
    server-render the location on the detail page, so the loop is pure cost —
    skip it, store the listing, and let the tagger infer location from the title.
    The tile layout never fetches details, so this flag doesn't affect it.
    """
    session = make_session()
    jobs: dict = {}
    total = None
    page_size = None
    start = 0
    MAX_PAGES = 40  # runaway guard (~2000 roles at 50/page)

    for _ in range(MAX_PAGES):
        # tal.net paginates by a `start` offset (50/page); the first page carries
        # no param so the exact single-page behaviour is preserved for the many
        # boards under one page.
        params = {"start": start} if start else {}
        response = session.get(board_url, params=params, headers=HEADERS, timeout=30)
        response.raise_for_status()
        fix_encoding(response)
        soup = BeautifulSoup(response.text, "html.parser")

        if total is None:
            total_el = soup.select_one(".results_meta h2")
            total_match = re.search(
                r"(\d[\d,]*)\s+results?", total_el.get_text(" ", strip=True)
            ) if total_el else None
            total = int(total_match.group(1).replace(",", "")) if total_match else 0

        # Two server-rendered layouts ship under tal.net/WCN. The classic one is
        # a <table.solr_search_list> (location only on the detail page → per-job
        # fetch). The newer "tile" layout (e.g. L.E.K.) puts each role in an
        # <li.opp-container> with the location inline (.candidate-opp-field-3),
        # so no detail fetch is needed. Detect the tile layout first and parse it
        # inline; otherwise fall back to the classic table path unchanged.
        tiles = soup.select("li.opp-container a.subject[href]")
        page_jobs = _parse_tiles(tiles) if tiles else _parse_table(
            soup, session, fetch_detail=fetch_detail)

        before = len(jobs)
        jobs.update(page_jobs)
        if page_size is None:
            page_size = len(page_jobs)

        # Stop when this page added nothing, or we've reached the reported total,
        # or there's no total to page against (single-page board).
        if len(jobs) == before or not page_jobs or not total or len(jobs) >= total:
            break
        start += page_size or len(page_jobs)

    if total and len(jobs) != total:
        raise RuntimeError(
            f"TAL.net board reported {total} jobs but exposed {len(jobs)}"
        )
    return list(jobs.values())


def _parse_tiles(tiles) -> dict:
    """Newer tile layout: location is inline, no detail fetch."""
    jobs = {}
    for link in tiles:
        url = link.get("href", "")
        match = re.search(r"/opp/(\d+)", url)
        if not match:
            continue
        job_id = match.group(1)
        if not url.startswith("http"):
            url = "https://" + url.lstrip("/") if "tal.net" in url else url
        location = ""
        tile = link.find_parent("li", class_="opp-container")
        if tile is not None:
            field = tile.select_one(".candidate-opp-field-3")
            if field is not None:
                label = field.select_one(".candidate-opp-field-label")
                if label is not None:
                    label.extract()  # drop the "Location:" label, keep the value
                location = field.get_text(" ", strip=True)
        jobs[job_id] = {
            "id": f"talnet_{job_id}",
            "title": link.get_text(" ", strip=True),
            "url": link.get("href", ""),
            "location": " ".join(location.split()),
            "posted": "",
        }
    return jobs


def _parse_table(soup, session, fetch_detail: bool = True) -> dict:
    """Classic table layout. Location lives on each detail page, so reading it
    costs one GET per job — skipped when ``fetch_detail`` is False (Oleeo
    Protect tenants), leaving location blank for the tagger to infer."""
    jobs = {}
    for link in soup.select("table.solr_search_list tr.details_row a.subject[href]"):
        url = link.get("href", "")
        match = re.search(r"/opp/(\d+)", url)
        if not match:
            continue
        job_id = match.group(1)
        location = ""
        if fetch_detail:
            detail = session.get(url, headers=HEADERS, timeout=30)
            detail.raise_for_status()
            fix_encoding(detail)
            detail_soup = BeautifulSoup(detail.text, "html.parser")
            for strong in detail_soup.find_all("strong"):
                if "location:" not in strong.get_text(" ", strip=True).lower():
                    continue
                parts = []
                for sibling in strong.next_siblings:
                    if getattr(sibling, "name", None) in {"br", "p"}:
                        break
                    text = sibling.get_text(" ", strip=True) if hasattr(
                        sibling, "get_text"
                    ) else str(sibling).strip()
                    if text:
                        parts.append(text)
                location = " ".join(parts).replace("\xa0", " ").strip()
                break
        jobs[job_id] = {
            "id": f"talnet_{job_id}",
            "title": link.get_text(" ", strip=True),
            "url": url,
            "location": " ".join(location.split()),
            "posted": "",
        }
    return jobs
