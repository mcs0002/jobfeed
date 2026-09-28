"""TalentView (talentview.io) description enricher.

The TalentView careers SPA is a JS shell — the job page at
``https://{company}.talentview.io/jobs/{slug}`` renders nothing useful over
plain HTTP, and the funnel LIST endpoint (see scrapers/talentview.py) carries
only title/location/type, not the body. The description lives at a separate
detail endpoint:

    GET https://api.talentview.io/funnel/v2/companies/{company}/campaigns/{slug}

with ONE catch that makes it look unscrapeable until you spot it: the endpoint
404s ("Resource not found.") unless the request carries an ``Origin`` header
matching the company's TalentView subdomain. With that header it returns the
full campaign record, including HTML ``description`` and ``profile`` fields.
(Referer alone does NOT satisfy it; Origin is the gate — confirmed 2026-07-01.)

Kept out of the generic GET path because the detail URL and the Origin header
both have to be derived from the stored ``/jobs/{slug}`` URL.
"""
import html
from urllib.parse import urlsplit

from .descriptions import _extract_text

API = "https://api.talentview.io/funnel/v2"


def is_talentview(url: str) -> bool:
    parts = urlsplit(url or "")
    segs = [s for s in parts.path.split("/") if s]
    return (parts.netloc.endswith(".talentview.io")
            and len(segs) >= 2 and segs[0] == "jobs")


def description(url: str, session, timeout: int = 15) -> str:
    """Plain-text description (body + candidate profile) for one TalentView job
    URL, or "" on any failure — callers keep going regardless."""
    parts = urlsplit(url or "")
    if not is_talentview(url):
        return ""
    company = parts.netloc.split(".talentview.io")[0]
    job_slug = [s for s in parts.path.split("/") if s][1]
    origin = f"{parts.scheme}://{parts.netloc}"
    try:
        r = session.get(
            f"{API}/companies/{company}/campaigns/{job_slug}",
            headers={
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0 Safari/537.36"),
                "Accept": "application/json",
                "Origin": origin,  # the gate — 404 without it
            },
            timeout=timeout,
        )
    except Exception:
        return ""
    if not r.ok:
        return ""
    try:
        payload = r.json()
    except ValueError:
        return ""
    node = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(node, dict):
        return ""
    desc = _extract_text(html.unescape(node.get("description") or ""))
    profile = _extract_text(html.unescape(node.get("profile") or ""))
    return (desc + ("\n\n" + profile if profile else "")).strip()
