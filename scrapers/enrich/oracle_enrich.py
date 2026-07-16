"""Fetch Oracle Recruiting Cloud (CE) job descriptions via the detail API.

The listing scraper (`scrapers/oracle_hcm.py`) only reads title/location/date
from `recruitingCEJobRequisitions`; the public job page is a JS shell, so a
plain GET enriches nothing. The full posting body lives behind a per-requisition
detail call on the same host:

    {base}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails
        ?finder=ById;Id={reqId},siteNumber={site}&expand=all&onlyData=true
    -> {"items": [{"ExternalDescriptionStr": "<html>...", ...}]}

Public, no auth, no session priming. Everything the call needs (host, site,
requisition id) is already in the job URL the scraper stored, so unlike the
Workday enricher this needs no config threaded in — parse it from the URL.

Note the finder takes a BARE id (`Id=210641279`), not a quoted one — the quoted
form the listing finder uses returns an empty item set here.
"""
import re

import requests

from .descriptions import _extract_text

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}

# https://<host>/hcmUI/CandidateExperience/en/sites/<site>/job/<reqId>[/...]
_URL_RE = re.compile(r"^(https?://[^/]+).*/sites/([^/]+)/job/(\d+)")

# Detail fields carrying the actual posting body, in reading order. The
# Corporate/Organization blurbs are firm boilerplate ("about us") — skip them;
# ExternalDescriptionStr is the role itself, with the responsibilities/
# qualifications fields appended when a tenant populates them separately.
_BODY_FIELDS = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                "ExternalQualificationsStr")


def is_oracle(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def description(url: str, session: requests.Session | None = None,
                timeout: int = 20) -> str:
    """Plain-text description for one Oracle CE job URL, or "" on any failure
    (expired requisition, network error, parse error)."""
    m = _URL_RE.match(url or "")
    if not m:
        return ""
    base_url, site, req_id = m.groups()
    getter = session or requests
    try:
        r = getter.get(
            f"{base_url}/hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitionDetails",
            params={
                "expand": "all",
                "onlyData": "true",
                "finder": f"ById;Id={req_id},siteNumber={site}",
            },
            headers=_HEADERS, timeout=timeout,
        )
        if r.status_code >= 400:
            return ""
        items = r.json().get("items", [])
    except (requests.RequestException, ValueError):
        return ""
    if not items:
        return ""
    item = items[0]
    html = "\n".join(item.get(f) or "" for f in _BODY_FIELDS if (item.get(f) or "").strip())
    return _extract_text(html)
