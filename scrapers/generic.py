"""
Generic HTML scraper using requests + BeautifulSoup.
Used for companies with custom career pages that don't have a public API.
Requires a CSS selector for the job listing elements.

Each target with ats="generic" needs:
  url: the career page URL
  selectors:
    container: CSS selector for each job item
    title: CSS selector for the job title (relative to container)
    link: CSS selector for the job link (relative to container, gets href)
    location: CSS selector for location (optional)
    title_sep: join title text nodes with this separator (optional; see below)
"""
import hashlib
from bs4 import BeautifulSoup

from ._http import curl_get, fix_encoding, make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def _fetch_soup(url: str, fetch: str) -> BeautifulSoup:
    if fetch == "curl":
        return BeautifulSoup(curl_get(url), "html.parser")
    if fetch == "cffi":
        # Cloudflare-gated pages that 403 plain requests but pass with
        # browser TLS impersonation (Horváth's TYPO3 site).
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(url, impersonate="chrome", timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    r = make_session().get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    fix_encoding(r)
    return BeautifulSoup(r.text, "html.parser")


def scrape(url: str, selectors: dict, company_slug: str = "",
           fetch: str = "requests") -> list[dict]:
    soup = _fetch_soup(url, fetch)

    container_sel = selectors.get("container")
    title_sel = selectors.get("title")
    link_sel = selectors.get("link")
    location_sel = selectors.get("location", "")

    if not container_sel or not title_sel:
        raise ValueError("selectors.container and selectors.title are required")

    items = soup.select(container_sel)
    # `page_param` (opt-in): the list is split across `?<param>=N` pages
    # (Altays: ODDO BHF's 236 offers, 10 a page). Walk from 2 until a page
    # holds no items or repeats the previous one (a site that ignores the
    # param would otherwise loop to `max_pages`).
    page_param = selectors.get("page_param")
    if page_param:
        prev = [str(i) for i in items]
        for n in range(2, int(selectors.get("max_pages", 60)) + 1):
            sep = "&" if "?" in url else "?"
            page = _fetch_soup(f"{url}{sep}{page_param}={n}", fetch).select(container_sel)
            cur = [str(i) for i in page]
            if not page or cur == prev:
                break
            items += page
            prev = cur
        else:
            raise RuntimeError(f"generic: pagination limit reached for {url}")

    jobs = []
    seen_ids = set()
    for item in items:
        title_el = item if title_sel == ":self" else item.select_one(title_sel)
        # `title_sep` joins a title split across text nodes ("M&A –<br>
        # Associate") with a space instead of gluing it ("M&A –Associate").
        # Opt-in per target: the id hashes the title, so changing the default
        # would re-key every existing generic row and churn a night of fake
        # new roles and delistings.
        title_sep = selectors.get("title_sep")
        if not title_el:
            title = ""
        elif title_sep:
            title = " ".join(title_el.get_text(title_sep, strip=True).split())
        else:
            title = title_el.get_text(strip=True)
        if not title:
            continue

        if link_sel == ":self":
            link_el = item
        else:
            link_el = item.select_one(link_sel) if link_sel else title_el
        # Some sites carry the detail URL in a non-href attribute (Horváth's
        # TYPO3 table rows use data-href on the <tr>); `link_attr` overrides.
        link_attr = selectors.get("link_attr", "href")
        href = link_el.get(link_attr, "") if link_el else ""
        # Accordion-style listings (Comgest) have no per-job anchor at all;
        # `url_fallback: "page"` points the row at the listing page itself.
        if not href and selectors.get("url_fallback") == "page":
            href = url
        if href and not href.startswith("http"):
            from urllib.parse import urljoin
            href = urljoin(url, href)

        location = ""
        if location_sel:
            loc_el = item.select_one(location_sel)
            location = (" ".join(loc_el.get_text(" ", strip=True).split())
                        if loc_el else "")

        # `description` (opt-in): the listing itself carries the posting text
        # when each role links out to a login wall or LinkedIn (Border to
        # Coast, Braemar); a body here skips the inline HTTP enrichment.
        description = ""
        desc_sel = selectors.get("description")
        if desc_sel:
            desc_el = item.select_one(desc_sel)
            description = desc_el.get_text("\n", strip=True) if desc_el else ""

        uid = hashlib.md5(f"{company_slug}_{title}_{href}".encode()).hexdigest()[:16]
        if uid in seen_ids:
            continue
        seen_ids.add(uid)
        jobs.append({
            "id": f"gen_{uid}",
            "title": title,
            "url": href,
            "location": location,
            "posted": "",
            **({"description": description} if description else {}),
        })

    if not jobs:
        # Fetched the page but the CSS container/title selector matched nothing.
        # For a plain HTML scrape there's usually no trustworthy "0 openings"
        # signal, so this is almost always a moved layout — raise rather than
        # return [] and have the delister purge the firm's stored rows.
        # Exception: a target may declare an `empty_marker` string (the site's
        # own explicit "no vacancies" prose, e.g. Arma Partners); if the fetched
        # page contains it, the board is trusted-empty.
        empty_marker = selectors.get("empty_marker", "")
        if empty_marker and empty_marker.lower() in soup.get_text(" ").lower():
            return []
        raise RuntimeError(
            f"generic: no jobs parsed from {url} "
            f"(container={container_sel!r})")
    return jobs
