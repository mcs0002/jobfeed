"""Avature server-rendered job search scraper."""
import re
import sys
import time
import concurrent.futures

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}

# Some tenants (Baloise) have NO listing-level location: the subtitle slot the
# location fallback reads holds only a posted date ("Posted 10-Jul-2026",
# span.list-item-posted). A date is not a location — rows were being stored
# with location='Posted 19-Jun-2026', which breaks the location tagger.
_POSTED_RE = re.compile(r"^posted\b", re.IGNORECASE)


def _location_text(el) -> str:
    """Location text from a card element, ignoring posted-date spans."""
    if el is None:
        return ""
    for posted in el.select(".list-item-posted"):
        posted.extract()
    text = el.get_text(" ", strip=True)
    return "" if _POSTED_RE.match(text) else text


def _fetch(search_url: str, page_size: int, offset: int, session):
    # Standard Avature portals page on job*; some self-hosted variants (Bain's
    # "FolderDetail" portal) page on folder*. Each ignores the other family, so
    # sending both makes paging work everywhere without per-portal config.
    response = session.get(
        search_url,
        params={
            "jobRecordsPerPage": page_size, "jobOffset": offset,
            "folderRecordsPerPage": page_size, "folderOffset": offset,
        },
        headers=HEADERS,
        timeout=25,
    )
    response.raise_for_status()
    fix_encoding(response)
    return BeautifulSoup(response.text, "html.parser")


def scrape(
    search_url: str,
    page_size: int = 9,
    follow_until_empty: bool = False,
) -> list[dict]:
    session = make_session()
    first = _fetch(search_url, page_size, 0, session)
    text = first.get_text(" ", strip=True)
    total_match = re.search(r"(\d[\d,]*)\s+(?:items|results)", text)
    first_cards = first.select(".article--result")
    effective_page_size = len(first_cards) or page_size
    if total_match:
        total = int(total_match.group(1).replace(",", ""))
        offsets = list(range(0, total, effective_page_size))
    else:
        offsets = [0]
        total = None
    soups = {0: first}
    if total is not None:
        # Fetch the remaining pages concurrently, but tolerate per-page
        # failures instead of letting one killed connection discard the whole
        # scrape. Some Avature servers (UniCredit) drop connections partway
        # through a fast concurrent walk; a failed page is retried once,
        # sequentially with a pause, since the drop is load-induced not a
        # dead page. Whatever still fails is left out and judged by the
        # completeness band below.
        failed: list[int] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(_fetch, search_url, page_size, offset, session): offset
                for offset in offsets[1:]
            }
            for future in concurrent.futures.as_completed(futures):
                offset = futures[future]
                try:
                    soups[offset] = future.result()
                except Exception:
                    failed.append(offset)
        for offset in failed:
            time.sleep(0.5)
            try:
                soups[offset] = _fetch(search_url, page_size, offset, session)
            except Exception:
                pass  # completeness band below decides if too much is missing
    else:
        offset = 0
        pending_offsets = set()
        while True:
            soup = soups[offset]
            cards = soup.select(".article--result")
            for link in soup.select('a[href*="jobOffset="]'):
                match = re.search(r"[?&]jobOffset=(\d+)", link.get("href", ""))
                if match and int(match.group(1)) > offset:
                    pending_offsets.add(int(match.group(1)))
            pending_offsets.difference_update(soups)
            if pending_offsets:
                next_offset = min(pending_offsets)
                pending_offsets.remove(next_offset)
            elif follow_until_empty and len(cards) == effective_page_size:
                next_offset = offset + effective_page_size
            else:
                break
            if next_offset in soups or len(offsets) >= 1000:
                break
            next_soup = _fetch(search_url, page_size, next_offset, session)
            if not next_soup.select(".article--result"):
                break
            offsets.append(next_offset)
            soups[next_offset] = next_soup
            offset = next_offset

    jobs = {}
    for offset in offsets:
        soup = soups.get(offset)
        if soup is None:
            continue  # page failed both attempts; completeness band handles it
        cards = soup.select(".article--result")
        for card in cards:
            link = None
            match = None
            # JobDetail is the standard Avature link; FolderDetail is the Bain
            # self-hosted portal variant. Both carry the numeric id last.
            for candidate in card.select(
                'a[href*="JobDetail"], a[href*="FolderDetail"]'
            ):
                href = candidate.get("href", "")
                direct_match = re.search(
                    r"/(?:Job|Folder)Detail/(?:[^/?#]+/)?(\d+)/?(?:\?|$)", href
                )
                if not direct_match:
                    direct_match = re.search(r"[?&]jobId=(\d+)", href)
                if direct_match:
                    link = candidate
                    match = direct_match
                    break
            if not link:
                continue
            href = link.get("href", "")
            details = [
                item.get_text(" ", strip=True)
                for item in card.select(".article__details__data p:last-child")
            ]
            location_el = (
                card.select_one(".job-info-icon_world")
                or card.select_one(".article__header__text__subtitle")
            )
            job_id = match.group(1)
            if len(details) > 1 and not _POSTED_RE.match(details[1]):
                location = details[1]
            else:
                location = _location_text(location_el)
            jobs[job_id] = {
                "id": f"avature_{job_id}",
                "title": link.get_text(" ", strip=True),
                "url": href,
                "location": " ".join(location.split()),
                "posted": "",
            }
    if total is not None and len(jobs) != total:
        if len(jobs) < 0.9 * total:
            raise RuntimeError(f"Avature reported {total} jobs but parsed {len(jobs)}")
        print(
            f"WARN Avature reported {total} jobs but parsed {len(jobs)}; "
            "keeping partial results",
            file=sys.stderr,
        )
    return list(jobs.values())
