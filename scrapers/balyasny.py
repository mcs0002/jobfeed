"""Balyasny Asset Management first-party careers feed.

The official careers board (linked from bamfunds.com/careers) is a Salesforce
Experience Cloud site at https://bambusdev.my.site.com/s/. The Lightning/Aura
frontend loads the complete vacancy inventory through a single guest-visible
Apex action, ``BamJobRequisitionInfoDataService.searchJobRequisitions``, which
returns every open requisition in one response (no pagination; the on-page
"N open positions" counter equals the row count).

The Aura endpoint only needs a framework uid and application hash that are
embedded in the public landing-page HTML, plus ``aura.token=null`` for guest
access. No cookies are required; each run derives a fresh context.
"""
import datetime
import json
import re

import requests

from ._http import make_session

BASE_URL = "https://bambusdev.my.site.com"
SHELL_URL = BASE_URL + "/s/"
AURA_URL = BASE_URL + "/s/sfsites/aura?r=1&aura.ApexAction.execute=1"
DETAIL_URL = BASE_URL + "/s/details?jobReq={slug}_{req}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

_FWUID_RE = re.compile(r'"fwuid":"([^"]+)"')
_LOADED_RE = re.compile(r'"APPLICATION@markup://siteforce:communityApp":"([^"]+)"')
_POSTED_RE = re.compile(r"Posted\s+(\d+)\s+Day", re.IGNORECASE)


def _aura_context(session: requests.Session) -> dict:
    """Derive a fresh Aura context from the public application shell."""
    response = session.get(SHELL_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    fwuid_match = _FWUID_RE.search(response.text)
    loaded_match = _LOADED_RE.search(response.text)
    if not fwuid_match or not loaded_match:
        raise RuntimeError("Balyasny: could not extract Aura context from shell HTML")
    return {
        "mode": "PROD",
        "fwuid": fwuid_match.group(1),
        "app": "siteforce:communityApp",
        "loaded": {
            "APPLICATION@markup://siteforce:communityApp": loaded_match.group(1)
        },
        "dn": [],
        "globals": {},
        "uad": True,
    }


def _posted_date(posted_ago: str) -> str:
    """Convert 'Posted N Days Ago' / 'Posted Today' to an ISO date string."""
    if not posted_ago:
        return ""
    lowered = posted_ago.lower()
    if "today" in lowered:
        return datetime.date.today().isoformat()
    if "yesterday" in lowered:
        return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    match = _POSTED_RE.search(posted_ago)
    if match:
        days = int(match.group(1))
        return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return ""


def _locations(record: dict) -> str:
    names = []
    for relation in ("Job_Requisition_Positions__r", "Job_Requisitions_Locations__r"):
        for entry in record.get(relation) or []:
            name = ((entry or {}).get("Location__r") or {}).get("External_Name__c")
            if name and name not in names:
                names.append(name)
    return ", ".join(names)


def scrape() -> list[dict]:
    session = make_session()
    context = _aura_context(session)
    action = {
        "id": "1;a",
        "descriptor": "aura://ApexActionController/ACTION$execute",
        "callingDescriptor": "UNKNOWN",
        "params": {
            "namespace": "",
            "classname": "BamJobRequisitionInfoDataService",
            "method": "searchJobRequisitions",
            "params": {
                "isVendorPortal": False,
                "site": "BAM Website",
                "searchKey": "",
                "locationFilters": [],
                "departmentFilter": [],
                "availableLocations": [],
                "experienceLevelFilter": [],
            },
            "cacheable": True,
            "isContinuation": False,
        },
    }
    data = {
        "message": json.dumps({"actions": [action]}),
        "aura.context": json.dumps(context),
        "aura.pageURI": "/s/",
        "aura.token": "null",
    }
    response = session.post(AURA_URL, data=data, headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    result = payload["actions"][0]
    if result.get("state") != "SUCCESS":
        raise RuntimeError(f"Balyasny: Aura action state {result.get('state')!r}")
    records = result["returnValue"]["returnValue"]

    jobs = []
    for record in records:
        req = record.get("Requisition_Number__c")
        title = (record.get("Publish_Title__c") or record.get("Name") or "").strip()
        slug = record.get("Job_Req_Title_in_URL__c")
        if not req or not title:
            continue
        if slug:
            url = DETAIL_URL.format(slug=slug, req=req)
        else:
            url = SHELL_URL
        jobs.append({
            "id": f"bam_{req}",
            "title": title,
            "url": url,
            "location": _locations(record),
            "posted": _posted_date(record.get("Posted_Ago__c") or ""),
        })
    return jobs
