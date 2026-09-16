"""Oracle Recruiting Cloud public Candidate Experience scraper."""
from ._http import assert_complete, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}
EXPAND = (
    "requisitionList.workLocation,"
    "requisitionList.otherWorkLocations,"
    "requisitionList.secondaryLocations"
)


def _scrape_keyword(base_url: str, site: str, keyword: str,
                    jobs: dict, session, page_size: int = 100) -> None:
    """Paginate one full-text keyword into the shared jobs dict (dedup by Id)."""
    kw_clause = f",keyword={keyword}" if keyword else ""
    offset = 0
    total = None
    # Unique ids fetched by THIS keyword's pagination, for the completeness
    # band. Not len(jobs): the shared dict accumulates across keywords, so
    # earlier keywords' rows would pad the count and mask a partial fetch.
    # Not offset either: a server that repeats the same page would inflate
    # offset to `total` while yielding one unique row.
    fetched_ids: set = set()
    while True:
        response = session.get(
            f"{base_url}/hcmRestApi/resources/latest/"
            "recruitingCEJobRequisitions",
            params={
                "onlyData": "true",
                "expand": EXPAND,
                "finder": (
                    f"findReqs;siteNumber={site}{kw_clause},limit={page_size},"
                    f"offset={offset},sortBy=POSTING_DATES_DESC"
                ),
            },
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        if not items:
            break

        result = items[0]
        total = int(result.get("TotalJobsCount", 0)) or None
        requisitions = result.get("requisitionList", [])

        # Page 1 empty but server reports jobs → session/schema issue.
        if not requisitions and offset == 0 and total:
            raise RuntimeError(
                f"Oracle HCM/{site}: TotalJobsCount={total} but page 1"
                " returned no requisitions"
            )

        for requisition in requisitions:
            job_id = str(requisition.get("Id", "")).strip()
            title = requisition.get("Title", "").strip()
            if not job_id or not title:
                continue
            fetched_ids.add(job_id)
            jobs[job_id] = {
                "id": f"oracle_{site}_{job_id}",
                "title": title,
                "url": (
                    f"{base_url}/hcmUI/CandidateExperience/en/sites/"
                    f"{site}/job/{job_id}"
                ),
                "location": requisition.get("PrimaryLocation", ""),
                "posted": requisition.get("PostedDate", ""),
            }

        offset += len(requisitions)
        if not requisitions or (total is not None and offset >= total):
            break

    # TotalJobsCount is scoped to the query (keyword or full board), so the
    # band compares against exactly what this pagination loop was meant to
    # fetch (see fetched_ids note above).
    assert_complete(len(fetched_ids), total, f"Oracle HCM/{site}")


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/")
    site = config["site"]
    # Giant tenants (JPMC = 7k roles) are run as "heavy" boards: pull the full
    # board (keyword="") and let the always-on negative filter (filter.py) shed
    # the ops/retail/IT bulk by title, keeping every finance division. A
    # `keyword` may still scope a smaller tenant server-side, but full-text
    # keyword matches descriptions, so it's a blunt instrument — prefer the
    # heavy + negative-filter path for the big ones.
    jobs: dict = {}
    session = make_session()
    _scrape_keyword(base_url, site, config.get("keyword", ""), jobs, session)
    return list(jobs.values())
