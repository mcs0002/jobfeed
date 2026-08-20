"""European Investment Bank Group PeopleSoft (Candidate Gateway, Fluid) scraper.

The EIB Group portal (erecruitment.eib.org) is an Oracle PeopleSoft HCM 9.2
Fluid Candidate Gateway. Unusually, the public job-search page renders the
ENTIRE results grid server-side on a plain cookie-free GET, including a
"<b>N</b> jobs found." total we can assert against, so no stateful ICAJAX
flow is needed.

Each row carries: SCH_JOB_TITLE$n, HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID$n,
LOCATION$n, HRS_BU_DESCR$n (business unit: "European Investment Bank" or
"European Investment Fund" - the portal is shared by the group) and
SCH_OPENED$n (posted date, dd/mm/yyyy).

Guest deep links to individual postings are disabled by EIB (they 302 back to
the search page), so the per-job URL is the official deep-link form which at
worst lands the user on the portal search list where the Job ID can be found.

Completeness guard: if the number of parsed rows ever differs from the
site-reported total (e.g. PeopleSoft starts lazy-loading the grid once the
count grows), we raise instead of silently returning a subset.

config = {
    "business_unit": "European Investment Bank",  # optional row filter;
                                                   # omit/None for all EIB Group jobs
}
"""
import html as html_lib
import re

from ._http import fix_encoding, make_session

SEARCH_URL = (
    "https://erecruitment.eib.org/psc/hr/EIBJOBS/CAREERS/c/"
    "HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
    "?Page=HRS_APP_SCHJOB_FL&Action=U&FOCUS=Applicant&SiteId=1"
)
JOB_URL = (
    "https://erecruitment.eib.org/psc/hr/EIBJOBS/CAREERS/c/"
    "HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
    "?Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId=1"
    "&JobOpeningId={job_id}&PostingSeq=1"
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en",
}

_FIELD = r"id='{name}\${row}' >([^<]*)<"


def _field(html: str, name: str, row: int) -> str:
    match = re.search(_FIELD.format(name=re.escape(name), row=row), html)
    if not match:
        return ""
    return html_lib.unescape(match.group(1)).strip()


def _iso_date(ddmmyyyy: str) -> str:
    match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", ddmmyyyy)
    if not match:
        return ddmmyyyy
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


def scrape(config: dict | None = None) -> list[dict]:
    config = config or {}
    business_unit = config.get("business_unit")

    response = make_session().get(SEARCH_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    fix_encoding(response)
    html = response.text

    total_match = re.search(r"<b>(\d+)</b>\s*jobs found", html)
    if not total_match:
        raise RuntimeError("EIB portal: could not find 'N jobs found' total "
                           "(page layout changed?)")
    total = int(total_match.group(1))

    row_indexes = sorted({
        int(m) for m in re.findall(r"id='HRS_AGNT_RSLT_I\$0_row_(\d+)'", html)
    })
    if len(row_indexes) != total:
        raise RuntimeError(
            f"EIB portal: parsed {len(row_indexes)} rows but site reports "
            f"{total} jobs - grid may have started lazy-loading; refusing "
            "to return a subset"
        )

    jobs = []
    for row in row_indexes:
        job_id = _field(html, "HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID", row)
        title = _field(html, "SCH_JOB_TITLE", row)
        if not job_id or not title:
            raise RuntimeError(f"EIB portal: row {row} missing job id/title")
        unit = _field(html, "HRS_BU_DESCR", row)
        if business_unit and unit != business_unit:
            continue
        jobs.append({
            "id": f"eib_{job_id}",
            "title": title,
            "url": JOB_URL.format(job_id=job_id),
            "location": _field(html, "LOCATION", row),
            "posted": _iso_date(_field(html, "SCH_OPENED", row)),
        })
    return jobs
