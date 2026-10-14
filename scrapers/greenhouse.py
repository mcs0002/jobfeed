"""
Greenhouse public API scraper.
Endpoint: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
No auth required — Greenhouse publishes this as a public job board API.
"""
import html
import json
import os
import re
import sys

from .enrich.descriptions import _extract_text  # noqa: E402
from ._http import make_session  # noqa: E402

BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(slug: str, eu: bool = False, url_template: str = "",
           departments: list[str] | None = None,
           exclude_departments: list[str] | None = None,
           site_bodies: str = "") -> list[dict]:
    # `departments` keeps a job listed under any of these department names;
    # `exclude_departments` drops a job only when EVERY department it lists is
    # excluded. A job with no department passes both, and a department the
    # firm adds later passes the exclude form, so prefer exclusion on boards
    # whose finance roles spread over several departments.
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
    # `site_bodies` names the firm's own careers page when it, not Greenhouse,
    # holds the posting text (Point72, see _salesforce_bodies).
    url = BASE.format(slug=slug)
    # content=true makes Greenhouse include the (HTML) description inline so
    # we can store it without a second per-job request.
    r = make_session().get(url, params={"content": "true"}, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = []
    keep, drop = set(departments or []), set(exclude_departments or [])
    for j in data.get("jobs", []):
        names = {d.get("name") for d in j.get("departments") or []}
        if keep and names and not names & keep:
            continue
        if drop and names and names <= drop:
            continue
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
    if site_bodies and any(len(j["description"]) < SHORT_BODY for j in jobs):
        bodies = _salesforce_bodies(site_bodies)
        for j in jobs:
            body = bodies.get(j["id"].removeprefix("gh_"), "")
            if len(j["description"]) < SHORT_BODY and len(body) > len(j["description"]):
                j["description"] = body
    return jobs


# A Greenhouse `content` this short is the title restated, not a posting.
SHORT_BODY = 200

_SF_INIT_RE = re.compile(r"CSSearchModule\.init\('((?:[^'\\]|\\.)*)'", re.S)
_GH_ID_RE = re.compile(r"greenhouse\.io/[^/]+/jobs/(\d+)")


def _salesforce_bodies(page_url: str) -> dict[str, str]:
    """Greenhouse job id -> posting text, read from a Salesforce careers site.

    Point72 keeps only the title in Greenhouse's `content` for some roles (11
    of 221, 2026-10-14); the body lives in careers.point72.com, whose single
    page embeds every Job__c record as a JS string passed to
    CSSearchModule.init, each with its Greenhouse apply link. Empty on any
    failure, so the Greenhouse text stands."""
    try:
        r = make_session().get(page_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        m = _SF_INIT_RE.search(r.text)
        if not m:
            return {}
        # A JS single-quoted literal: drop the escapes JSON lacks (\', \&),
        # read it as a JSON string, then parse the JSON it holds.
        literal = re.sub(r"\\([^\"\\/bfnrtu])", r"\1", m.group(1))
        records = json.loads(json.loads(f'"{literal}"'))
    except Exception:
        return {}
    out = {}
    for rec in records:
        job = (rec.get("job") if isinstance(rec, dict) else None) or {}
        gh = _GH_ID_RE.search(job.get("Apply_Now_URL__c") or "")
        text = _extract_text(job.get("Job_Description_External__c") or "")
        if gh and text:
            out[gh.group(1)] = text
    return out
