"""Goldman Sachs first-party "Higher" careers GraphQL feed.

The public career portal at https://higher.gs.com is a Next.js app backed by
an unauthenticated GraphQL gateway at https://api-higher.gs.com. The
``roleSearch`` query powers both the professional board (/results, experiences
EARLY_CAREER + PROFESSIONAL) and the campus/student board (/campus,
experience CAMPUS). No cookies or tokens are required.
"""
import time

from ._http import make_session

API_URL = "https://api-higher.gs.com/gateway/api/v1/graphql"
ROLE_URL = "https://higher.gs.com/roles/{role_id}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; job-scraper/1.0)",
    "Content-Type": "application/json",
    "Origin": "https://higher.gs.com",
    "Referer": "https://higher.gs.com/results",
}
# External candidate experiences (INTERNAL_MOBILITY is employees-only).
# Queried per group exactly like the site: /campus uses CAMPUS, /results
# uses EARLY_CAREER + PROFESSIONAL. Each group's totalCount is verified
# independently; a combined query reports the summed count but dedupes
# items, which breaks the completeness check.
DEFAULT_EXPERIENCE_GROUPS = [
    ["CAMPUS"],
    ["EARLY_CAREER", "PROFESSIONAL"],
]
PAGE_SIZE = 100

QUERY = """
query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items {
      roleId
      jobTitle
      division
      lastPostedDate
      locations {
        primary
        city
        state
        country
      }
    }
  }
}
"""


def _format_location(locations: list[dict]) -> str:
    if not locations:
        return ""
    primary = next(
        (loc for loc in locations if loc.get("primary")), locations[0]
    )
    parts = [primary.get("city") or "", primary.get("country") or ""]
    location = ", ".join(part for part in parts if part)
    if len(locations) > 1:
        location += f" (+{len(locations) - 1} more)"
    return location


def _scrape_experiences(session, experiences: list[str], jobs: dict) -> None:
    seen = set()
    page_number = 0  # pageNumber is zero-indexed
    total = None

    while True:
        response = session.post(
            API_URL,
            json={
                "operationName": "GetRoles",
                "query": QUERY,
                "variables": {
                    "searchQueryInput": {
                        "experiences": experiences,
                        "sort": {
                            "sortStrategy": "POSTED_DATE",
                            "sortOrder": "DESC",
                        },
                        "page": {
                            "pageNumber": page_number,
                            "pageSize": PAGE_SIZE,
                        },
                    }
                },
            },
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL error: {payload['errors']}")

        result = payload["data"]["roleSearch"]
        total = int(result["totalCount"])
        items = result.get("items") or []

        for item in items:
            role_id = str(item.get("roleId") or "").strip()
            title = (item.get("jobTitle") or "").strip()
            if not role_id or not title:
                continue
            seen.add(role_id)
            posted = (item.get("lastPostedDate") or "")[:10]
            jobs[role_id] = {
                "id": f"gs_{role_id}",
                "title": title,
                "url": ROLE_URL.format(role_id=role_id),
                "location": _format_location(item.get("locations") or []),
                "posted": posted,
            }

        if len(seen) >= total or not items:
            break
        page_number += 1
        time.sleep(0.5)

    # Tolerate tiny shortfalls from postings appearing/expiring mid-crawl,
    # but still reject genuinely partial feeds.
    if total is not None:
        tolerance = max(2, int(total * 0.01))
        if len(seen) < total - tolerance:
            raise RuntimeError(
                f"Incomplete Goldman feed for {experiences}: "
                f"collected {len(seen)} of {total}"
            )


def scrape(config: dict | None = None) -> list[dict]:
    groups = (config or {}).get(
        "experience_groups", DEFAULT_EXPERIENCE_GROUPS
    )
    jobs = {}
    session = make_session()
    for experiences in groups:
        _scrape_experiences(session, experiences, jobs)
    return list(jobs.values())
