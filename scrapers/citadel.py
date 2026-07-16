"""
Citadel / Citadel Securities first-party careers scraper.

Both www.citadel.com and www.citadelsecurities.com run the same WordPress
careers listing behind Cloudflare. Plain `requests` gets a 403, but the
Cloudflare wall here is a TLS/JA3 fingerprint check, NOT a real JS challenge:
`curl_cffi` with Chrome impersonation passes it and returns the fully
server-rendered listing — no browser needed. (Re-probed 2026-07-01: curl_cffi
clears both domains, all pages, and the detail pages; the prior Playwright
implementation is retired. This is the Koch/Tikehau lesson — a Cloudflare 403
is a JA3 wall until proven a real challenge.)

The listing paginates at /careers/open-opportunities/page/{n}/ with 10 jobs
per page and reports the grand total in <span class="total-post">, which
lets us prove completeness on every run. The listing covers all sections
including Internships and New Graduates; the separate "programs & events"
content is intentionally not part of the official Open Opportunities feed.

config = {
    "base_url": "https://www.citadel.com",   # or https://www.citadelsecurities.com
    "prefix": "citadel",                      # id prefix, e.g. "citsec"
}
"""
import math
import sys

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

LISTING_PATH = "/careers/open-opportunities/"
PAGE_SIZE = 10


def _fetch_listing_page(session, url: str) -> str:
    """Load one listing URL via curl_cffi (Chrome JA3 impersonation clears the
    Cloudflare fingerprint wall). Raises on a non-200."""
    r = session.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"{url}: HTTP {r.status_code} (Cloudflare not cleared)")
    return r.text


def _parse_listing(html: str, prefix: str) -> tuple[list[dict], int | None]:
    soup = BeautifulSoup(html, "html.parser")

    total = None
    total_el = soup.select_one("span.total-post")
    if total_el and total_el.get_text(strip=True).isdigit():
        total = int(total_el.get_text(strip=True))

    jobs = []
    for card in soup.select("a.careers-listing-card"):
        url = card.get("href", "")
        if "/careers/details/" not in url:
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        title_el = card.select_one(".careers-listing-card__title h2")
        title = (
            title_el.get_text(strip=True)
            if title_el
            else card.get("data-position", "").strip()
        )
        loc_el = card.select_one(".careers-listing-card__location")
        location = " ".join(loc_el.get_text(" ", strip=True).split()) if loc_el else ""
        if not slug or not title:
            continue
        jobs.append({
            "id": f"{prefix}_{slug}",
            "title": title,
            "url": url,
            "location": location,
            "posted": "",
        })
    return jobs, total


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/")
    prefix = config.get("prefix", "citadel")
    listing_url = base_url + LISTING_PATH

    session = creq.Session(impersonate="chrome124")
    seen: dict[str, dict] = {}

    html = _fetch_listing_page(session, listing_url)
    jobs, total = _parse_listing(html, prefix)
    if total is None:
        raise RuntimeError(f"{listing_url}: total job count not found")
    for job in jobs:
        seen.setdefault(job["id"], job)

    # Walk every listing page. _fetch_listing_page raises on any non-200, so
    # reaching the end of this loop proves we saw the whole listing — the only
    # way the unique count falls short of `total` is Citadel emitting duplicate
    # cards (same role, shared /careers/details/ slug), which dedups here.
    last_page = math.ceil(total / PAGE_SIZE)
    for n in range(2, last_page + 1):
        html = _fetch_listing_page(session, f"{listing_url}page/{n}/")
        jobs, _ = _parse_listing(html, prefix)
        for job in jobs:
            seen.setdefault(job["id"], job)

    # Completeness check with the same 90% tolerance band as avature/attrax: a
    # large shortfall is a real miss (raise), a small one is dedup (WARN, keep).
    if len(seen) < 0.9 * total:
        raise RuntimeError(
            f"{listing_url}: collected {len(seen)} unique jobs "
            f"but site reports {total}")
    if len(seen) != total:
        print(
            f"WARN Citadel reported {total} cards but parsed {len(seen)} "
            "unique jobs (duplicate listings); keeping all",
            file=sys.stderr,
        )
    return list(seen.values())
