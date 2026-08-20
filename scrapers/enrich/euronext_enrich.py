"""Fetch Euronext job descriptions from the Drupal job page's body field.

The listing scraper (`scrapers/euronext.py`) returns no description. The job
pages ARE server-rendered Drupal, but the generic backstop stores the wrong
thing either way: the page's JSON-LD JobPosting ``description`` is only the
~300-500 char teaser paragraph (that's what `_jobposting_description` finds and
stores — e.g. euronext_28006 stuck at 523 chars), and when JSON-LD is skipped
the whole-page strip is ~18k chars of nav/footer chrome that hits the 16k cap.

The full posting body lives in the page's Drupal body field::

    <div class="... field--name-body ...">  (first substantial occurrence)

The page carries three such containers (the ad body, an empty one, a
social-links one), so we take the first whose extracted text is non-trivial.
Public, no auth, plain GET on the stored URL. Confirmed 2026-07-16: 3687 chars
(r28006) / 3338 chars (r27226) vs. the 523/278-char stored teasers.
"""
import re

import requests

from .descriptions import _extract_text, HEADERS

# https://www.euronext.com/en/about/careers/job-offers/r28006-france-...
_URL_RE = re.compile(
    r"^https?://(?:www\.)?euronext\.com/.+/job-offers/", re.I)

_BODY_OPEN_RE = re.compile(r'<div[^>]+class="[^"]*field--name-body[^"]*"[^>]*>',
                           re.I)
_DIV_RE = re.compile(r"<(/?)div\b[^>]*?(/?)>", re.I)

# The social-links body container strips to ~26 chars; a real posting is
# thousands. Anything under this is chrome, not the ad.
_MIN_BODY_CHARS = 200


def is_euronext(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def _balanced_div(page: str, start: int) -> str:
    """The <div>...</div> block beginning at start, nesting-aware."""
    depth = 0
    for m in _DIV_RE.finditer(page, start):
        if m.group(2):          # self-closing <div/> — no depth change
            continue
        if m.group(1):          # </div>
            depth -= 1
            if depth == 0:
                return page[start:m.end()]
        else:
            depth += 1
    return ""


def description(url: str, session: requests.Session | None = None,
                timeout: int = 15) -> str:
    """Plain-text description for one Euronext job URL, or "" on any failure."""
    if not is_euronext(url):
        return ""
    getter = session or requests
    try:
        r = getter.get(url, headers=HEADERS, timeout=timeout,
                       allow_redirects=True)
        if r.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    for m in _BODY_OPEN_RE.finditer(r.text):
        block = _balanced_div(r.text, m.start())
        text = _extract_text(block)
        if len(text) >= _MIN_BODY_CHARS:
            return text
    return ""
