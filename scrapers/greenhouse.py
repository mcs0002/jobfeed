"""
Greenhouse public API scraper.
Endpoint: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
No auth required — Greenhouse publishes this as a public job board API.
"""
import html
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .enrich.descriptions import _extract_text  # noqa: E402
from ._http import make_session  # noqa: E402

BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(slug: str, eu: bool = False, url_template: str = "") -> list[dict]:
    # `url_template` overrides the API's `absolute_url` with
    # template.format(id=...) — for boards whose absolute_url points at a firm
    # site path that no longer exists (the firm redesigned; Greenhouse still
    # serves the stale template and even boards.greenhouse.io redirects into
    # it). Found by link_health: Mako and GSA Capital both 404'd this way.
    # `eu` is accepted for back-compat with targets.json's `eu: true` flag but
    # unused: boards-api.eu.greenhouse.io doesn't resolve (confirmed dead via
    # DNS + curl, 2026-07-01) — the EU-hosted career-page UI
    # (job-boards.eu.greenhouse.io) is real, but its API is served from the
    # standard boards-api.greenhouse.io host regardless. EQT/Permira (the
    # only two `eu: true` targets) both 200 there.
    url = BASE.format(slug=slug)
    # content=true makes Greenhouse include the (HTML) description inline so
    # we can store it without a second per-job request.
    r = make_session().get(url, params={"content": "true"}, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"gh_{j['id']}",
            "title": j.get("title", ""),
            "url": (url_template.format(id=j["id"]) if url_template
                    else j.get("absolute_url", "")),
            "location": j.get("location", {}).get("name", ""),
            "posted": j.get("updated_at", "")[:10],
            # Greenhouse returns the description as entity-encoded HTML
            # (e.g. "&lt;p&gt;...&lt;/p&gt;"). Unescape entities into real tags,
            # then strip to clean text so it's readable + good LLM input.
            "description": _extract_text(html.unescape(j.get("content", "") or "")),
        })
    return jobs
