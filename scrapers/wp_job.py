"""WordPress REST 'job' custom-post-type scraper (curl-fetched).

Some firms (Mercuria) self-host vacancies as a WordPress custom post type served
by the public WP REST API: ``GET {base_url}/wp-json/wp/v2/{post_type}``. Each
item carries ``title.rendered``, ``link`` (absolute) and ``content.rendered``
(full HTML description).

Fetched via the system ``curl`` binary (``_http.curl_get``), NOT ``requests``:
mercuria.com ships an incomplete TLS chain (missing intermediate) that
requests/certifi and curl_cffi reject, but macOS curl completes it via AIA.
Pagination is by page-fill (request ``page`` until a short page) so we don't
need the ``X-WP-TotalPages`` response header (curl returns the body only).
"""
import json
from html import unescape

from ._http import curl_get


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "base_url": "https://mercuria.com",
        "post_type": "job",      # WP custom post type
        "page_size": 100,
        "tenant": "mercuria",    # optional label for job IDs
    }
    """
    base_url = config["base_url"].rstrip("/")
    post_type = config.get("post_type", "job")
    page_size = config.get("page_size", 100)
    tenant = config.get("tenant", "wp")
    endpoint = f"{base_url}/wp-json/wp/v2/{post_type}"

    jobs = {}
    page = 1
    while True:
        try:
            body = curl_get(f"{endpoint}?per_page={page_size}&page={page}")
        except RuntimeError as e:
            # WP's REST API answers a page past the last with HTTP 400
            # (rest_post_invalid_page_number) — end-of-corpus when we already
            # paged through results, not a failure. Anything else propagates:
            # a swallowed outage would read as "board empty" and delist the
            # whole source.
            if page > 1 and "error: 400" in str(e):
                break
            raise
        try:
            items = json.loads(body)
        except ValueError as e:
            raise RuntimeError(
                f"wp_job: non-JSON body from {endpoint} page {page} "
                f"(WAF challenge?): {body[:120]!r}") from e
        if not isinstance(items, list) or not items:
            break

        for it in items:
            job_id = it.get("id")
            title = unescape(((it.get("title") or {}).get("rendered") or "").strip())
            if not job_id or not title:
                continue
            jobs[job_id] = {
                "id": f"wp_{tenant}_{job_id}",
                "title": title,
                "url": it.get("link", ""),
                "location": "",
                "description": (it.get("content") or {}).get("rendered", ""),
                "posted": str(it.get("date", ""))[:10],
            }

        if len(items) < page_size:
            break
        page += 1
        if page > 50:  # runaway guard
            break

    return list(jobs.values())
