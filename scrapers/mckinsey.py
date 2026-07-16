"""McKinsey & Company — global-search gateway API.

The Avature portalpack (jobs.mckinsey.com / mckinsey.avature.net) is the
WAF-walled surface that earned McKinsey its "manual" verdict — but it was the
wrong surface. The actual careers search lives on www.mckinsey.com as a
Next.js app whose backend is a plain JSON gateway with no bot gate at all:
one GET returns the whole board (591 postings at discovery, 2026-07-06) with
descriptions inline. Found by reading the page's static JS chunk: the SPA
builds ``{host}/apigw-{id}/v1/api/jobs/search`` from ``__NEXT_DATA__``
runtimeConfig.

The gateway id can rotate with site deploys, so on any failure the scraper
re-derives host+id from ``__NEXT_DATA__`` on the careers page. That page
fetch needs curl_cffi — www.mckinsey.com itself sits behind an Akamai TLS
gate that drops plain clients; the gateway host does not.
"""
import json
import re

import requests

from .enrich.descriptions import _extract_text

DEFAULT_HOST = "https://gateway.mckinsey.com"
DEFAULT_GATEWAY_ID = "x0cceuow60"
SEARCH_PAGE = "https://www.mckinsey.com/careers/search-jobs"
JOB_URL = "https://www.mckinsey.com/careers/search-jobs/jobs/{friendly}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": SEARCH_PAGE}
PAGE_SIZE = 1000
# Postings are often firm-wide (an "Associate" req lists ~100 cities); keep
# the stored location string readable while staying honest about the spread.
MAX_CITIES = 4


def _derive_gateway() -> tuple[str, str]:
    from curl_cffi import requests as cffi
    r = cffi.get(SEARCH_PAGE, impersonate="chrome", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"mckinsey: search page HTTP {r.status_code}")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("mckinsey: __NEXT_DATA__ not found on search page")
    cfg = json.loads(m.group(1))["runtimeConfig"]
    return (cfg["API_HOST_GLOBAL_SEARCH_EXTERNALIZED_SERVICE"],
            cfg["GLOBAL_SEARCH_API_GATEWAY_ID"])


def _fetch_page(host: str, gateway_id: str, start: int) -> dict:
    r = requests.get(
        f"{host}/apigw-{gateway_id}/v1/api/jobs/search",
        params={"pageSize": PAGE_SIZE, "start": start},
        headers=HEADERS, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def scrape() -> list[dict]:
    try:
        first = _fetch_page(DEFAULT_HOST, DEFAULT_GATEWAY_ID, 0)
        host, gateway_id = DEFAULT_HOST, DEFAULT_GATEWAY_ID
    except Exception:
        host, gateway_id = _derive_gateway()
        first = _fetch_page(host, gateway_id, 0)

    docs = list(first.get("docs", []))
    total = int(first.get("numFound", 0))
    while len(docs) < total:
        more = _fetch_page(host, gateway_id, len(docs)).get("docs", [])
        if not more:
            break
        docs.extend(more)

    jobs = []
    for d in docs:
        job_id = str(d.get("jobID", "")).strip()
        friendly = (d.get("friendlyURL") or "").strip()
        if not job_id or not friendly:
            continue
        cities = [c for c in (d.get("cities") or []) if c]
        location = ", ".join(cities[:MAX_CITIES])
        if len(cities) > MAX_CITIES:
            location += f" (+{len(cities) - MAX_CITIES} more)"
        body = " ".join(part for part in (d.get("whatYouWillDo"),
                                          d.get("yourBackground")) if part)
        jobs.append({
            "id": f"mck_{job_id}",
            "title": d.get("title", ""),
            "url": JOB_URL.format(friendly=friendly),
            "location": location,
            "posted": "",
            "description": _extract_text(body) if body else "",
        })
    if not jobs:
        # A firm this size is never at 0 — an empty result means the gateway
        # moved or the schema changed, and returning [] would delist the board.
        raise RuntimeError("mckinsey: gateway returned no jobs")
    return jobs
