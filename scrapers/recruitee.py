"""Recruitee public careers API scraper.

Endpoint: https://{slug}.recruitee.com/api/offers/
No auth — Recruitee publishes the company board as a public JSON API. The
listing payload already includes the (HTML) description, so no per-job fetch.
"""
import html
import os
import sys

from ._http import make_session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from .enrich.descriptions import _extract_text  # noqa: E402

BASE = "https://{slug}.recruitee.com/api/offers/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(slug: str) -> list[dict]:
    r = make_session().get(BASE.format(slug=slug), headers=HEADERS, timeout=20)
    r.raise_for_status()
    jobs = []
    for j in r.json().get("offers", []):
        if j.get("status") and j["status"] != "published":
            continue
        desc = j.get("description") or ""
        jobs.append({
            "id": f"recruitee_{j['id']}",
            "title": j.get("title", ""),
            "url": j.get("careers_url") or j.get("careers_apply_url", ""),
            "location": j.get("location") or ", ".join(
                v for v in (j.get("city", ""), j.get("country", "")) if v
            ),
            "posted": (j.get("created_at") or j.get("published_at") or "")[:10],
            "description": _extract_text(html.unescape(desc)) if desc else "",
        })
    return jobs
