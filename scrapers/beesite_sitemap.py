"""BeeSite (milch & zucker) self-hosted board scraper via sitemap.

Some self-hosted BeeSite installs (e.g. KfW, jobs.kfw.de) render the search
result list client-side and expose no ``api-*.beesite.de`` JSON endpoint, so the
API-based ``beesite`` handler can't reach them. But the sitemap lists every ad
as ``index.php?ac=jobad&id={N}`` and each ad page IS server-rendered. This
adapter enumerates the sitemap and fetches each ad for title + body (one GET per
open role — BeeSite boards are mid-sized). Title from ``og:title``/``<h1>``,
body from the ``.jobad`` container.
"""
import json
import re

from bs4 import BeautifulSoup

from ._http import make_session, fix_encoding

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
_ID = re.compile(r"[?&]id=(\d+)")


def _clean_title(text: str) -> str:
    # KfW's <title>/og:title is "Stellenanzeige: <role>" — strip the prefix.
    return re.sub(r"^\s*Stellenanzeige:\s*", "", text or "").strip()


def _iter_jobpostings(soup):
    """Yield every schema.org JobPosting object in the page's JSON-LD blocks.
    BeeSite ads embed one (sometimes wrapped in a list); tolerate malformed
    blocks by skipping them."""
    for tag in soup.select('script[type="application/ld+json"]'):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                yield obj


def _location_from_ld(posting: dict) -> str:
    """City-level location string from a JobPosting's jobLocation. Joins the
    distinct localities of multi-site ads (the tagger then normalizes/picks the
    primary). Falls back to region/country if no locality is given."""
    loc = posting.get("jobLocation")
    entries = loc if isinstance(loc, list) else [loc] if loc else []
    parts, seen = [], set()
    for e in entries:
        addr = (e or {}).get("address") or {}
        if not isinstance(addr, dict):
            continue
        place = (addr.get("addressLocality") or addr.get("addressRegion")
                 or addr.get("addressCountry") or "")
        place = str(place).strip()
        if place and place.lower() not in seen:
            seen.add(place.lower())
            parts.append(place)
    return ", ".join(parts)


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "sitemap_url": "https://jobs.kfw.de/sitemap.xml",
        "prefix": "kfw",
        "body_selector": ".jobad",   # optional, defaults to .jobad
    }
    """
    sitemap_url = config["sitemap_url"]
    prefix = config["prefix"]
    body_selector = config.get("body_selector", ".jobad")
    session = make_session()

    resp = session.get(sitemap_url, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    urls = re.findall(r"<loc>(.*?)</loc>", resp.text)
    ad_urls = []
    seen = set()
    for raw in urls:
        url = raw.replace("&amp;", "&")
        m = _ID.search(url)
        if "ac=jobad" in url and m and m.group(1) not in seen:
            seen.add(m.group(1))
            ad_urls.append((m.group(1), url))
    if not ad_urls:
        raise RuntimeError(f"beesite_sitemap: no jobad URLs in {sitemap_url}")

    jobs = {}
    for job_id, url in ad_urls:
        try:
            page = session.get(url, headers=HEADERS, timeout=30)
            page.raise_for_status()
            fix_encoding(page)
        except Exception:
            continue  # skip a single bad ad, keep the rest (sitemap already proved the board is live)
        soup = BeautifulSoup(page.text, "html.parser")
        og = soup.select_one('meta[property="og:title"]')
        title = _clean_title(
            (og.get("content") if og else None)
            or (soup.h1.get_text(strip=True) if soup.h1 else ""))
        if not title:
            continue
        body_el = soup.select_one(body_selector)
        posting = next(_iter_jobpostings(soup), {})
        jobs[job_id] = {
            "id": f"beesite_{prefix}_{job_id}",
            "title": title,
            "url": url,
            "location": _location_from_ld(posting),
            "description": body_el.get_text(" ", strip=True) if body_el else "",
            "posted": (posting.get("datePosted") or "")[:10],
        }
    if not jobs:
        # The sitemap listed jobad URLs (checked above) but no ad page yielded a
        # title — the ad-page markup (og:title / <h1>) moved. Raise rather than
        # return [] and delist the firm.
        raise RuntimeError(f"beesite_sitemap: no ads parsed from {prefix}")
    return list(jobs.values())
