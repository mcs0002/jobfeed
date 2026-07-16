"""Sitemap-derived job scraper for static/JS sites with title-bearing URLs.

Some bespoke career sites (e.g. DPAM / Degroof Petercam, a Gatsby+Contentful
build) render listings client-side but publish every job page in the XML
sitemap at a slug that already spells out the role, e.g.
``/en-be/careers/2451-investment-banking-associate``. Rather than fetch each
(here a 1.3 MB Gatsby page-data.json bundling the whole site), we take the job
URLs straight from the sitemap and derive the title from the slug. Listing-only
by design (NONE body strategy); the descriptive slug carries the signal.
"""
import re

from ._http import make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
_LOC = re.compile(r"<loc>(.*?)</loc>")


def _titleize(slug: str) -> str:
    # some sites encode punctuation in the slug (e.g. A&M: -and- -> &, -slash- -> /)
    slug = slug.replace("-slash-", "/").replace("-and-", " & ")
    words = slug.replace("-", " ").replace("_", " ").split()
    out = []
    for w in words:
        # keep parenthetical location casing tidy: "(hasselt)" -> "(Hasselt)"
        if w.startswith("(") and len(w) > 1:
            out.append("(" + w[1:].capitalize())
        else:
            out.append(w if w.isupper() else w.capitalize())
    return " ".join(out)


def _fetch_sitemap(fetch, url: str) -> list[str]:
    text = fetch(url)
    return [u.replace("&amp;", "&") for u in _LOC.findall(text)]


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "sitemap_url": "https://www.degroofpetercam.com/sitemap_en-be/sitemap-0.xml",
        "path_contains": "/en-be/careers/",
        "prefix": "dpam",
    }
    ``sitemap_url`` may be a <sitemapindex> — one level of nested sitemaps is
    followed automatically.
    """
    sitemap_url = config["sitemap_url"]
    needle = config["path_contains"]
    prefix = config["prefix"]
    exclude = config.get("exclude_contains")
    require_id = config.get("require_id", False)  # only URLs whose last segment is {digits}-slug

    if config.get("fetch") == "cffi":
        from curl_cffi import requests as _cffi

        def fetch(url):
            r = _cffi.get(url, impersonate="chrome", timeout=40)
            r.raise_for_status()
            return r.text
    else:
        session = make_session()

        def fetch(url):
            r = session.get(url, headers=HEADERS, timeout=40)
            r.raise_for_status()
            return r.text

    urls = _fetch_sitemap(fetch, sitemap_url)
    # follow one level of sitemap index if the top level held no job URLs
    if not any(needle in u for u in urls):
        nested = [u for u in urls if u.endswith(".xml")]
        expanded = []
        for sm in nested:
            try:
                expanded += _fetch_sitemap(fetch, sm)
            except Exception:
                continue
        urls = expanded or urls

    jobs = {}
    for url in urls:
        if needle not in url or (exclude and exclude in url):
            continue
        segment = url.rstrip("/").rsplit("/", 1)[-1]
        if config.get("trailing_id"):
            m = re.match(r"(.+)-(\d+)$", segment)  # slug-{id} (e.g. APG)
            slug, job_id = (m.group(1), m.group(2)) if m else (segment, segment)
            if require_id and not m:
                continue
        else:
            m = re.match(r"(\d+)-(.+)", segment)   # {id}-slug (e.g. DPAM)
            if m:
                job_id, slug = m.group(1), m.group(2)
            elif require_id:
                continue  # skip facet/search URLs that lack a real numeric job id
            else:
                job_id, slug = segment, segment
        if job_id in jobs:
            continue
        jobs[job_id] = {
            "id": f"{prefix}_{job_id}",
            "title": _titleize(slug),
            "url": url,
            "location": "",
            "description": "",
            "posted": "",
        }
    if not jobs:
        raise RuntimeError(
            f"sitemap_jobs: no URLs matching {needle!r} in {sitemap_url}")
    return list(jobs.values())
