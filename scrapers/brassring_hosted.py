"""Client-hosted IBM/Infinite BrassRing Talent Gateway scraper.

Some BrassRing customers (e.g. UBS at jobs.ubs.com) serve the Talent Gateway
from their own domain instead of sjobs.brassring.com.  The existing
``scrapers/brassring.py`` hardcodes the sjobs.brassring.com AJAX host, so it
cannot paginate those sites.  This module derives the AJAX host from the
configured search URL and works cookie-free with plain ``requests``:

1. GET the ``HomeWithPreLoad`` search page; it embeds a ``preloadResponse``
   JSON blob with page 1 of the results plus an encrypted session value.
2. POST ``/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs`` (same host, same
   requests session) for pages 2..N until an empty page is returned.
3. Assert the number of fetched postings matches the server-reported
   ``JobsCount`` so partial feeds fail loudly.

Postings may be duplicated per requisition for translated languages; results
are deduplicated by ``reqid`` preferring the English (language id "1") copy.
"""
import json
import sys
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ENGLISH_LANGUAGE_ID = "1"
MAX_PAGES = 200


def _questions(job: dict) -> dict:
    return {
        item.get("QuestionName", "").lower(): item.get("Value", "")
        for item in job.get("Questions", [])
    }


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "search_url": "https://jobs.ubs.com/TGnewUI/Search/Home/HomeWithPreLoad"
                      "?partnerid=25008&siteid=5012&PageType=searchResults"
                      "&SearchType=linkquery&LinkID=0",
        "partner_id": 25008,
        "site_id": 5012,
        "link_id": 0,                      # optional, default 0
        "location_field": "formtext23",    # optional question field for location
    }
    """
    search_url = config["search_url"]
    partner_id = str(config["partner_id"])
    site_id = str(config["site_id"])
    link_id = str(config.get("link_id", 0))
    location_field = config.get("location_field", "formtext23")

    parts = urlsplit(search_url)
    ajax_url = (
        f"{parts.scheme}://{parts.netloc}"
        "/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"
    )

    session = make_session()
    response = session.get(search_url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    fix_encoding(response)
    soup = BeautifulSoup(response.text, "html.parser")
    preload = soup.find(attrs={"capture-escaped-parsed-value": "preloadResponse"})
    if preload is None:
        raise RuntimeError("BrassRing page did not expose preload data")

    payload = json.loads(preload["value"])
    criteria = json.loads(payload["SmartSearchJSONValue"])
    search_results = payload["searchResultsResponse"]
    cookie_value = soup.select_one("#CookieValue")
    encrypted_session = (
        cookie_value.get("value", "") if cookie_value else
        criteria.get("EncryptedSessionValue", "")
    )
    if not encrypted_session:
        raise RuntimeError("BrassRing page did not expose a session value")

    postings = list((search_results.get("Jobs") or {}).get("Job", []))
    reported_total = 0

    for page_number in range(2, MAX_PAGES + 1):
        page_response = session.post(
            ajax_url,
            json={
                "partnerId": partner_id,
                "siteId": site_id,
                "keyword": "",
                "location": "",
                "keywordCustomSolrFields": criteria.get(
                    "KeywordCustomSolrFields", ""
                ),
                "locationCustomSolrFields": criteria.get(
                    "LocationCustomSolrFields", ""
                ),
                "facetfilterfields": {"Facet": []},
                "linkId": link_id,
                "Latitude": 0,
                "Longitude": 0,
                "sortby": None,
                "powersearchoptions": {"PowerSearchOption": []},
                "SortType": None,
                "pageNumber": page_number,
                "encryptedSessionValue": encrypted_session,
            },
            headers={**HEADERS, "Referer": search_url},
            timeout=60,
        )
        page_response.raise_for_status()
        page = page_response.json()
        reported_total = int(page.get("JobsCount") or reported_total)
        page_jobs = (page.get("Jobs") or {}).get("Job", [])
        if not page_jobs:
            break
        postings.extend(page_jobs)
        time.sleep(0.5)
    else:
        raise RuntimeError("BrassRing pagination exceeded MAX_PAGES")

    if reported_total and len(postings) != reported_total:
        if len(postings) < 0.9 * reported_total:
            raise RuntimeError(
                f"BrassRing reported {reported_total} postings "
                f"but fetched {len(postings)}"
            )
        print(
            f"WARN BrassRing reported {reported_total} postings "
            f"but fetched {len(postings)}; keeping partial results",
            file=sys.stderr,
        )

    jobs = {}
    for item in postings:
        fields = _questions(item)
        job_id = str(fields.get("reqid", "")).strip()
        title = str(fields.get("jobtitle", "")).strip()
        if not job_id or not title:
            continue
        language = str(fields.get("jobreqlanguage", "")).strip()
        if job_id in jobs and language != ENGLISH_LANGUAGE_ID:
            continue  # keep first/English copy of translated requisitions
        location = str(fields.get(location_field, "")).strip()
        if not location:
            location = ", ".join(
                value.strip() for value in (
                    fields.get("formtext8", ""),
                    fields.get("formtext10", ""),
                )
                if value.strip()
            )
        jobs[job_id] = {
            "id": f"brassring_{partner_id}_{job_id}",
            "title": title,
            "url": item.get("Link", ""),
            "location": location,
            "posted": str(fields.get("lastupdated", "")).strip(),
        }
    return list(jobs.values())
