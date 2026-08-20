"""Kernel ATS scraper (api.apptrkr.io).

Kernel exposes clean public JSON per recruitment campaign:

    GET https://api.apptrkr.io/api/v1/{company_slug}/{campaign_slug}
    -> data.vacancies[]

There is NO stable company-wide listing endpoint — only per-campaign slugs, and
the campaign slug has the academic year baked in (e.g. ``…-202526``), so a
hardcoded slug rots annually. We avoid that by DISCOVERING the live slug at
runtime: the firm's careers page (a Next.js app) embeds the apptrkr URL in one
of its static JS chunks, so we fetch the page, follow it to the chunk, and pull
the current slug out. When the firm rolls to the next year the chunk just points
at the new slug and the chain self-heals. Plain HTTP — the JS bundle is static
text we regex, not executed (policy-compliant, no browser).

Config (in targets.json under ``kernel``):
    company_slug   stable Kernel company token (e.g. "rokos-capital-management")
    careers_url    the firm's careers page to discover the campaign slug from
    campaign_slug  optional explicit override (skips discovery)
"""
import re

from ._http import make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
}
API = "https://api.apptrkr.io/api/v1/{company}/{campaign}"
_CHUNK_RE = re.compile(r"/_next/static/chunks/[^\"']+\.js")


def _discover_slug(careers_url: str, company_slug: str, session) -> str | None:
    """Find the live campaign slug embedded in the careers page's JS bundle."""
    slug_re = re.compile(
        re.escape(company_slug) + r"/([a-z0-9][a-z0-9-]*)")
    page = session.get(careers_url, headers=HEADERS, timeout=30)
    page.raise_for_status()
    # The apptrkr URL may sit directly in the HTML, else in a referenced chunk.
    for haystack in [page.text] + _fetch_chunks(careers_url, page.text, session):
        m = slug_re.search(haystack)
        if m:
            return m.group(1)
    return None


def _fetch_chunks(careers_url: str, page_html: str, session) -> list[str]:
    from urllib.parse import urljoin
    bodies = []
    for path in dict.fromkeys(_CHUNK_RE.findall(page_html)):
        try:
            r = session.get(urljoin(careers_url, path), headers=HEADERS, timeout=30)
            if r.ok:
                bodies.append(r.text)
        except Exception:
            continue
    return bodies


def scrape(config: dict) -> list[dict]:
    company = config["company_slug"]
    session = make_session()

    campaign = config.get("campaign_slug")
    if not campaign:
        campaign = _discover_slug(config["careers_url"], company, session)
    if not campaign:
        # Raise, don't return [] — an empty result here reads downstream as
        # "board is empty" and delists every stored row for the firm. Slug
        # discovery failing usually means the careers page was redesigned.
        raise RuntimeError(f"KERNEL_SLUG_NOT_FOUND company={company}")

    resp = session.get(
        API.format(company=company, campaign=campaign), headers=HEADERS, timeout=30)
    resp.raise_for_status()
    vacancies = (resp.json().get("data") or {}).get("vacancies") or []

    jobs = []
    for v in vacancies:
        if str(v.get("status", "1")) not in ("1",):  # "1" == live
            continue
        vid = str(v.get("id") or v.get("slug") or "")
        title = v.get("title") or ""
        if not vid or not title:
            continue
        vslug = v.get("slug") or vid
        jobs.append({
            "id": f"kernel_{vid}",
            "title": title,
            "url": f"https://apptrkr.io/{company}/{campaign}/vacancy/{vslug}",
            "location": "",  # Kernel exposes no structured location; lives in title
            "posted": str(v.get("created_at") or "")[:10],
            "description": v.get("description") or "",
        })
    return jobs
