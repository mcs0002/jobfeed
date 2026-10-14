"""iCIMS Jibe public jobs API scraper."""
from urllib.parse import urljoin

from ._http import assert_complete, make_session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)"}


def scrape(config: dict) -> list[dict]:
    base_url = config["base_url"].rstrip("/") + "/"
    page = 1
    jobs = {}
    total = None
    session = make_session()

    while True:
        params = {
            "page": page,
            "limit": config.get("page_size", 20),
            "sortBy": "relevance",
            "descending": "false",
            "internal": "false",
        }
        if config.get("domain"):
            params["domain"] = config["domain"]
        # Server-side facet scope (e.g. AXA's tags3 entity facet: "GIE AXA"
        # cuts the 1.5k-role group board to the ~12 holding-level ALM/
        # investment roles). Keys are passed through verbatim.
        params.update(config.get("extra_params", {}))
        response = session.get(
            urljoin(base_url, "api/jobs"),
            params=params,
            headers=HEADERS,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("jobs", [])
        if not items:
            break

        before = len(jobs)
        for item in items:
            data = item.get("data", {})
            job_id = str(data.get("req_id") or data.get("slug") or "").strip()
            title = data.get("title", "").strip()
            if not job_id or not title:
                continue
            jobs[job_id] = {
                "id": f"jibe_{config.get('company_id', 'company')}_{job_id}",
                "title": title,
                "url": _job_url(base_url, job_id, data.get("language")),
                "location": data.get("full_location") or data.get("short_location", ""),
                "posted": data.get("posted_date", "")[:10],
                "_excluded": _excluded(data, config.get("exclude_values")),
            }

        if "totalCount" not in payload:
            raise RuntimeError(
                "Jibe API response missing 'totalCount' field — schema drift"
            )
        total = payload["totalCount"]
        if len(jobs) == before:
            raise RuntimeError(
                f"Jibe: page={page} repeated with no new posting ids"
            )
        if len(jobs) >= total:
            break
        page += 1

    # Pagination and completeness count every posting; exclusion applies after.
    assert_complete(len(jobs), total, "Jibe")
    return [j for j in jobs.values() if not j.pop("_excluded")]


def _job_url(base_url: str, job_id: str, language: str | None) -> str:
    """The posting's page in the language it was written in. Without `lang`
    a multi-language board answers 404 for a posting with no English version:
    AXA's German and French roles (de-de, fr-fr) did, so the link was dead and
    the page's JSON-LD body never reached enrichment (2026-10-14)."""
    url = urljoin(base_url, f"jobs/{job_id}")
    return f"{url}?lang={language}" if language else url


def _excluded(data: dict, exclude: dict | None) -> bool:
    """`exclude_values` = {field: [values]}, e.g. {"tags2": ["Technology"]}:
    drop a posting when, for some field, every value it lists is excluded. A
    posting with no value for the field passes, as does a value the firm adds
    later. Client-side because Jibe's own filters take one value per field."""
    for field, values in (exclude or {}).items():
        raw = data.get(field) or []
        listed = {v.get("name") if isinstance(v, dict) else v
                  for v in (raw if isinstance(raw, list) else [raw])}
        if listed and listed <= set(values):
            return True
    return False
