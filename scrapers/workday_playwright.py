"""
Workday scraper using Playwright to handle 422/CSRF-protected instances.

Many major banks (GS, JPM, MS, Deutsche, BNP, UBS, HSBC etc.) block cold API
calls with a 422. Their Workday requires a valid browser session before the
POST endpoint accepts requests.

Strategy:
1. Load the career page in a headless browser to get session cookies + CSRF token
2. Intercept the actual /jobs API call the page makes (gets us the correct URL too)
3. Reuse those credentials for paginated fetches

Slower than the direct API (~5-10s per company vs ~1s) but reliable.
"""
import json
import time
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from ._http import assert_complete, make_session

# How long to wait for the page to make its first jobs API call
PAGE_LOAD_TIMEOUT = 20_000  # ms
INTERCEPT_TIMEOUT = 15_000  # ms


def scrape(config: dict) -> list[dict]:
    """
    config = {
        "tenant": "goldmansachs",
        "version": "wd1",
        "board": "External_Career_Site",
        "career_url": "https://www.goldmansachs.com/careers/"  # page to load for session
    }
    """
    tenant = config["tenant"]
    version = config.get("version", "wd1")
    board = config["board"]
    career_url = config.get("career_url", f"https://{tenant}.{version}.myworkdayjobs.com/{board}")

    api_base = f"https://{tenant}.{version}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )

        intercepted_request = {}

        def handle_request(request):
            if "/wday/cxs/" in request.url and "/jobs" in request.url:
                if not intercepted_request:
                    intercepted_request["url"] = request.url
                    intercepted_request["headers"] = dict(request.headers)
                    intercepted_request["post_data"] = request.post_data

        page = context.new_page()
        page.on("request", handle_request)

        try:
            page.goto(career_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
            # Wait a bit for JS to fire the jobs API call
            page.wait_for_timeout(5000)
        except PlaywrightTimeout:
            pass  # page might be slow, we still have cookies

        cookies = context.cookies()
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        browser.close()

    # Build headers from intercepted request or fall back to defaults
    if intercepted_request.get("headers"):
        base_headers = intercepted_request["headers"]
        # Override with the actual API URL if discovered
        if intercepted_request.get("url"):
            api_base = intercepted_request["url"].split("?")[0]
            # Strip offset param to use our own pagination
    else:
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Workday-Client": "2023.43.4",
        }

    if cookie_header:
        base_headers["Cookie"] = cookie_header

    # Now paginate with requests (much faster than Playwright per page)
    session = make_session()

    jobs = []
    offset = 0
    limit = 20
    total = None
    seen_paths = set()

    while True:
        payload = {
            "appliedFacets": {},
            "limit": limit,
            "offset": offset,
            "searchText": "",
        }

        r = session.post(api_base, json=payload, headers=base_headers, timeout=20)
        r.raise_for_status()
        data = r.json()

        postings = data.get("jobPostings", [])
        if total is None:
            total = data.get("total", 0)
        if not postings:
            # Page 1 is empty but the server says there are jobs — something is
            # wrong (bad session, CSRF failure, schema change).  Fail loud.
            if total:
                raise RuntimeError(
                    f"Workday/{tenant}: total={total} but page 1 returned no postings"
                )
            break

        base_site = f"https://{tenant}.{version}.myworkdayjobs.com"
        for j in postings:
            path = j.get("externalPath", "")
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            job_url = f"{base_site}/{quote(board, safe='_-')}{path}"
            jobs.append({
                "id": f"wd_{tenant}_{path.split('/')[-1]}",
                "title": j.get("title", ""),
                "url": job_url,
                "location": j.get("locationsText", ""),
                "posted": j.get("postedOn", ""),
            })

        offset += limit
        if total is not None and offset >= total:
            break

    assert_complete(len(jobs), total, f"Workday/{tenant}")
    return jobs
