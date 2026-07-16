"""European Investment Bank (erecruitment.eib.org) description enricher.

EIB's PeopleSoft Fluid portal disables guest deep-links — a GET of a job URL
302s back to the search page, so the generic enricher only ever sees ~7 chars.
But the posting body IS reachable over plain HTTP via the stateful ICAJAX POST
the page uses to open a row's detail panel:

  1. GET the search page (server-renders the whole grid + hidden ICSID/ICStateNum).
  2. Map JobOpeningId -> grid row index from the `HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID$<row>` fields.
  3. POST the component with ICAction=HRS_VIEW_DETAILS$<row> (+ every hidden
     field + ICSID/ICStateNum from that GET — the state is per-GET).
  4. The response embeds the rich-text body in `HRS_SCH_PSTDSC_DESCRLONG$0`.

No browser. ~2 requests/job (re-GET per call so the ICSID/ICStateNum match).
"""
import html as html_lib
import re

import requests
from bs4 import BeautifulSoup

from .descriptions import _extract_text

_HOST_RE = re.compile(r"^https?://erecruitment\.eib\.org/", re.I)
_JOBID_RE = re.compile(r"[?&]JobOpeningId=(\d+)")
_COMPONENT = ("https://erecruitment.eib.org/psc/hr/EIBJOBS/CAREERS/c/"
              "HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL")
_SEARCH = _COMPONENT + "?Page=HRS_APP_SCHJOB_FL&Action=U&FOCUS=Applicant&SiteId=1"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en",
}


def is_eib(url: str) -> bool:
    return bool(_HOST_RE.match(url or "")) and bool(_JOBID_RE.search(url or ""))


def _hidden_fields(soup) -> dict:
    data = {}
    for inp in soup.select("input[type=hidden][name]"):
        data[inp["name"]] = inp.get("value", "")
    return data


def description(url: str, session: requests.Session | None = None,
                timeout: int = 20) -> str:
    """Plain-text posting body for one EIB job URL, or "" on any failure."""
    m = _JOBID_RE.search(url or "")
    if not m or not is_eib(url):
        return ""
    job_id = m.group(1)
    getter = session or requests.Session()
    try:
        g = getter.get(_SEARCH, headers=_HEADERS, timeout=timeout)
        g.raise_for_status()
    except requests.RequestException:
        return ""
    grid = g.text

    # JobOpeningId -> row index.
    row = None
    for rm in re.finditer(
            r"id='HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID\$(\d+)' >([^<]*)<", grid):
        if html_lib.unescape(rm.group(2)).strip() == job_id:
            row = rm.group(1)
            break
    if row is None:
        return ""

    soup = BeautifulSoup(grid, "html.parser")
    data = _hidden_fields(soup)
    data.update({
        "ICAJAX": "1",
        "ICType": "Panel",
        "ICStateNum": data.get("ICStateNum", "1"),
        "ICAction": f"HRS_VIEW_DETAILS${row}",
    })
    try:
        p = getter.post(_COMPONENT, data=data, timeout=timeout, headers={
            **_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        })
        p.raise_for_status()
    except requests.RequestException:
        return ""

    # The ICAJAX response is XML whose CDATA sections carry the HTML. The body
    # is the value span id=HRS_SCH_PSTDSC_DESCRLONG$0 (unquoted id, nested tags),
    # so pull the CDATA, parse it, and select the node by id.
    cdata = "".join(re.findall(r"<!\[CDATA\[(.*?)\]\]>", p.text, re.S))
    if not cdata:
        return ""
    node = BeautifulSoup(cdata, "html.parser").find(
        id="HRS_SCH_PSTDSC_DESCRLONG$0")
    return _extract_text(str(node)) if node else ""
