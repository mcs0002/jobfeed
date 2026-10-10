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
    campaign_pattern  optional, e.g. "rokos-capital-management-20{y1}{y2}":
                   when the careers page no longer embeds the slug (Rokos,
                   2026-08), try the academic-year campaigns (20{y1}{y2} = "202526")
                   for last, this and next year and union every one that
                   exists, since a new campaign opens before the old one closes.

Deleted 2026-09-23 as unreachable and restored 2026-09-30: Rokos still runs
campaign rokos-capital-management-202526 (open to 2027-07-01) with its 2027
graduate programme, although rokoscapital.com stopped linking it.
"""
import datetime
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


def _campaigns(config: dict, company: str, session) -> list[str]:
    if config.get("campaign_slug"):
        return [config["campaign_slug"]]
    if config.get("careers_url"):
        try:
            slug = _discover_slug(config["careers_url"], company, session)
        except Exception:
            slug = None
        if slug:
            return [slug]
    pattern = config.get("campaign_pattern")
    if not pattern:
        return []
    year = datetime.date.today().year % 100
    return [pattern.format(y1=f"{y:02d}", y2=f"{y + 1:02d}")
            for y in (year + 1, year, year - 1)]


def scrape(config: dict) -> list[dict]:
    company = config["company_slug"]
    session = make_session()

    vacancies, found = [], []
    for campaign in _campaigns(config, company, session):
        resp = session.get(
            API.format(company=company, campaign=campaign), headers=HEADERS, timeout=30)
        if resp.status_code == 404:
            # "Campaign is not active." = the slug exists but the firm closed
            # it (Rokos 202526, 2026-10-10): a real, empty board, not a moved
            # one. Main's clean-zero backstop keeps it out of the delist pass.
            if "not active" in resp.text.lower():
                found.append(campaign)
            continue
        resp.raise_for_status()
        found.append(campaign)
        for v in (resp.json().get("data") or {}).get("vacancies") or []:
            vacancies.append((campaign, v))
    if not found:
        # Raise, don't return [] — an empty result here reads downstream as
        # "board is empty" and delists every stored row for the firm. No
        # campaign found usually means the firm moved its board.
        raise RuntimeError(f"KERNEL_SLUG_NOT_FOUND company={company}")

    jobs = []
    for campaign, v in vacancies:
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
