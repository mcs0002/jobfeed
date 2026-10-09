"""Balyasny (Salesforce Aura) description enricher — plain HTTP, no browser.

Balyasny's careers site (bambusdev.my.site.com, a Salesforce Experience Cloud
Aura app) renders the job body only after JS runs, which is why descriptions
used to go through the headless-browser enricher. But the detail page's own
Aura calls are reproducible over plain HTTP (discovered 2026-07-01 by capturing
the page's XHR in a browser — discovery via browser is policy-OK, the scraper
stays HTTP):

  1. ``BamJobRequisitionInfoDataService.searchJobRequisitions`` — the same guest
     Apex action the listing scraper already uses; returns every requisition
     with its Salesforce record ``Id`` and ``Requisition_Number__c`` (REQ####).
  2. ``rolePage.getDescription`` with ``{req: <recordId>}`` — returns
     ``{Description__c: "<html>…"}``, the full posting body, for guests.

So: one search call builds a ``REQ#### -> recordId`` map (cached per enricher
instance), then one getDescription per row. This retired the last Playwright
scraping path — playwright_enrich.py (which drove a headless browser for
exactly this one site) was deleted 2026-07-01. See tests/test_playwright_allowlist.py.
"""
import html
import json
import re

from .descriptions import _extract_text
from scrapers.balyasny import AURA_URL, HEADERS, _aura_context
from scrapers._http import make_session

_DETAILS_RE = re.compile(r"^https?://bambusdev\.my\.site\.com/s/details", re.IGNORECASE)
_REQ_RE = re.compile(r"_(REQ\d+)\b", re.IGNORECASE)


def is_balyasny(url: str) -> bool:
    return bool(_DETAILS_RE.match(url or ""))


class BalyasnyEnricher:
    """Lazily builds+caches the requisition-number -> record-Id map from one
    searchJobRequisitions call, then serves getDescription per URL. Mirrors the
    CsodEnricher/WorkdayEnricher shape (one instance reused across rows)."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self._session = None
        self._ctx = None
        self._req_to_id: dict | None = None

    def _aura(self, classname: str, method: str, params: dict):
        if self._session is None:
            self._session = make_session()
            self._ctx = _aura_context(self._session)
        action = {
            "id": "1;a",
            "descriptor": "aura://ApexActionController/ACTION$execute",
            "callingDescriptor": "UNKNOWN",
            "params": {"namespace": "", "classname": classname, "method": method,
                       "params": params, "cacheable": False, "isContinuation": False},
        }
        data = {
            "message": json.dumps({"actions": [action]}),
            "aura.context": json.dumps(self._ctx),
            "aura.pageURI": "/s/details",
            "aura.token": "null",
        }
        r = self._session.post(AURA_URL, data=data, headers=HEADERS, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["actions"][0]

    def _map(self) -> dict:
        if self._req_to_id is None:
            try:
                res = self._aura(
                    "BamJobRequisitionInfoDataService", "searchJobRequisitions",
                    {"isVendorPortal": False, "site": "BAM Website", "searchKey": "",
                     "locationFilters": [], "departmentFilter": [],
                     "availableLocations": [], "experienceLevelFilter": []})
                recs = res["returnValue"]["returnValue"]
                self._req_to_id = {
                    r.get("Requisition_Number__c"): r.get("Id")
                    for r in recs if r.get("Requisition_Number__c") and r.get("Id")
                }
            except Exception:
                self._req_to_id = {}
        return self._req_to_id

    def description(self, url: str) -> str:
        """Plain-text description for one Balyasny detail URL, or "" on failure."""
        m = _REQ_RE.search(url or "")
        if not m:
            return ""
        record_id = self._map().get(m.group(1).upper())
        if not record_id:
            return ""
        try:
            res = self._aura("rolePage", "getDescription", {"req": record_id})
            body = (res.get("returnValue") or {}).get("returnValue") or {}
            return _extract_text(html.unescape(body.get("Description__c") or ""))
        except Exception:
            return ""
