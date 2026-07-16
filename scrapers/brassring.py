"""IBM BrassRing public search scraper."""
import json
import math

from bs4 import BeautifulSoup

from ._http import fix_encoding, make_session


HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def _questions(job: dict) -> dict:
    return {
        item.get("QuestionName", "").lower(): item.get("Value", "")
        for item in job.get("Questions", [])
    }


def scrape(config: dict) -> list[dict]:
    search_url = config["search_url"]
    session = make_session()
    response = session.get(search_url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    fix_encoding(response)
    soup = BeautifulSoup(response.text, "html.parser")
    preload = soup.find(attrs={"capture-escaped-parsed-value": "preloadResponse"})
    if preload is None:
        raise RuntimeError("BrassRing page did not expose preload data")

    payload = json.loads(preload["value"])
    criteria = json.loads(payload["SmartSearchJSONValue"])
    search_results = payload["searchResultsResponse"]
    total = int(search_results.get("JobsCount") or payload["TotalCount"])
    first_jobs = (search_results.get("Jobs") or {}).get("Job", [])
    page_size = int(
        search_results.get("PageSize", 0)
        or len(first_jobs)
        or 50
    )
    pages = math.ceil(total / page_size)
    all_pages = [search_results]
    cookie_value = soup.select_one("#CookieValue")
    encrypted_session = (
        cookie_value.get("value", "") if cookie_value else
        criteria.get("EncryptedSessionValue", "")
    )

    for page_number in range(2, pages + 1):
        page_response = session.post(
            "https://sjobs.brassring.com/TgNewUI/Search/Ajax/"
            "ProcessSortAndShowMoreJobs",
            json={
                "partnerId": str(config["partner_id"]),
                "siteId": str(config["site_id"]),
                "keyword": "",
                "location": "",
                "keywordCustomSolrFields": criteria.get(
                    "KeywordCustomSolrFields", ""
                ),
                "locationCustomSolrFields": criteria.get(
                    "LocationCustomSolrFields", ""
                ),
                "facetfilterfields": {"Facet": []},
                "linkId": str(config.get("link_id", 0)),
                "Latitude": 0,
                "Longitude": 0,
                "sortby": None,
                "powersearchoptions": {"PowerSearchOption": []},
                "SortType": None,
                "pageNumber": page_number,
                "encryptedSessionValue": encrypted_session,
            },
            headers={**HEADERS, "Referer": search_url},
            timeout=45,
        )
        page_response.raise_for_status()
        all_pages.append(page_response.json())

    jobs = {}
    for page in all_pages:
        for item in (page.get("Jobs") or {}).get("Job", []):
            fields = _questions(item)
            job_id = str(fields.get("reqid", "")).strip()
            title = str(fields.get("jobtitle", "")).strip()
            if not job_id or not title:
                continue
            jobs[job_id] = {
                "id": f"brassring_{job_id}",
                "title": title,
                "url": item.get("Link", ""),
                "location": ", ".join(
                    value for value in (
                        fields.get("formtext8", ""),
                        fields.get("formtext10", ""),
                    )
                    if value
                ),
                "posted": fields.get("lastupdated", ""),
            }

    if len(jobs) != total:
        raise RuntimeError(f"BrassRing reported {total} jobs but parsed {len(jobs)}")
    return list(jobs.values())
