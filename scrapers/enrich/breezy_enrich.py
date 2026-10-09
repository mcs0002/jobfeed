"""Fetch Breezy HR job descriptions, stripping unrendered i18n tokens.

Breezy serves the position page as an SPA: the public HTML already carries the
full posting body, but a handful of UI-chrome strings are left as raw i18n
template placeholders (`%HEADER_EMPLOYEES%`, `%BUTTON_APPLY_TO_POSITION%`,
`%FOOTER_POWERED_BY%`, ...) that only get substituted client-side after JS runs.
The generic enricher (`enrich_descriptions.enrich_one`) therefore stores a body
littered with `%TOKEN%` noise.

No clean JSON exists for the body: the company feed at `https://<co>.breezy.hr/
json` lists positions (id/name/url/location/...) but carries NO `description`
field, and the per-position `.../json` paths just redirect back to the SPA HTML.
So the fix is a token-strip pass, not an API swap: GET the page, run the same
`_extract_text` everything else uses, then drop the bare-placeholder lines.

The real body text itself never contains `%[A-Z0-9_]+%` tokens, so stripping is
safe — the placeholders only ever appear as standalone chrome labels.
"""
import re

import requests

from .descriptions import _extract_text, HEADERS

# https://<company>.breezy.hr/p/<hexid>[-slug...]
_URL_RE = re.compile(r"^https?://[^/]+\.breezy\.hr/p/[0-9a-f]+", re.I)

# A line that is ENTIRELY one (or more whitespace-separated) i18n placeholders,
# e.g. "%HEADER_EMPLOYEES%" or "%BUTTON_APPLY_USING_INDEED%". Anchored to the
# whole line so we never gut a real sentence that merely contains a token.
_TOKEN_LINE_RE = re.compile(r"^(?:%[A-Z0-9_]+%\s*)+$")
# Defensive: also scrub any stray inline token the body might carry.
_INLINE_TOKEN_RE = re.compile(r"%[A-Z0-9_]+%")


def is_breezy(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def _strip_tokens(text: str) -> str:
    """Remove unrendered %TOKEN% chrome from extracted text."""
    lines = [ln for ln in text.splitlines() if not _TOKEN_LINE_RE.match(ln.strip())]
    out = "\n".join(lines)
    out = _INLINE_TOKEN_RE.sub("", out)        # belt-and-suspenders
    out = re.sub(r"\n{3,}", "\n\n", out)       # re-cap blank runs after removal
    return out.strip()


def description(url: str, session: requests.Session | None = None,
                timeout: int = 12) -> str:
    """Plain-text description for one Breezy position URL with NO `%TOKEN%`
    placeholders, or "" on any failure (expired position, network/parse error)."""
    if not is_breezy(url):
        return ""
    getter = session or requests
    try:
        r = getter.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    return _strip_tokens(_extract_text(r.text))
