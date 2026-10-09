"""Fetch Zoho Recruit job descriptions from the career-page hydration blob.

The listing scraper (`scrapers/zoho_recruit.py`) reads the published-jobs list
from the careers page's inline JSON, but that list carries no body — only
``Posting_Title`` / ``City`` / ``id`` (hence the ~57-char stored stubs). The
per-job page (``/jobs/Careers/{id}``) is an SPA, BUT its initial HTML embeds the
SAME jobs array with the full ``Job_Description`` field, HTML-entity-encoded and
wrapped in a ``JSON.parse('[{\\x22...}]')`` JS string literal.

We unescape the HTML entities, locate the ``[{"Salary"...}]`` literal, decode
its JS escapes (``\\xNN``, ``\\uNNNN``, ``\\/`` — while keeping the inner
``\\\\"`` HTML quotes escaped so the outer JSON stays valid), JSON-parse it, and
pull the matching record's ``Job_Description``. Public, no auth. The numeric job
id is in the stored URL. Confirmed 2026-07-16: 2347 chars vs. ~57 for the stub.
"""
import html
import re

import requests

from .descriptions import _extract_text

_HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0 Safari/537.36")}

# https://<portal>.zohorecruit.<tld>/jobs/Careers/<numeric-id>
_URL_RE = re.compile(
    r"^https?://[^/]+\.zohorecruit\.[a-z.]+/jobs/Careers/(\d+)", re.I)

# The embedded jobs array literal starts at [{\x22Salary\x22 (post entity-decode).
_ARR_START = r"[{\x22Salary\x22"


def is_zoho_recruit(url: str) -> bool:
    return bool(_URL_RE.match(url or ""))


def _decode_js_literal(lit: str) -> str:
    """Turn a JS string literal (the arg to JSON.parse) into JSON text.

    Zoho encodes the JSON's own structural quotes as ``\\xNN`` and the inner
    HTML quotes as ``\\\\xNN`` (an escaped backslash then the hex quote). We
    decode ``\\\\`` -> ``\\`` first so those inner quotes survive as escaped
    ``\\"`` in the resulting JSON; structural ``\\x22`` becomes a bare ``"``."""
    out = []
    i = 0
    n = len(lit)
    while i < n:
        c = lit[i]
        if c == "\\" and i + 1 < n:
            nxt = lit[i + 1]
            if nxt == "x":
                out.append(chr(int(lit[i + 2:i + 4], 16)))
                i += 4
                continue
            if nxt == "u":
                out.append(chr(int(lit[i + 2:i + 6], 16)))
                i += 6
                continue
            if nxt == "/":
                out.append("/")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
            if nxt == '"':
                out.append('"')
                i += 2
                continue
            if nxt in "nrt":
                out.append("\\" + nxt)
                i += 2
                continue
            out.append(nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _slice_array(text: str, start: int) -> str:
    """Balanced [...] slice from start, string-aware (the literal still holds
    escaped quotes at this stage, so track them)."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def description(url: str, session: requests.Session | None = None,
                timeout: int = 15) -> str:
    """Plain-text description for one Zoho Recruit job URL, or "" on any
    failure."""
    m = _URL_RE.match(url or "")
    if not m:
        return ""
    import json
    job_id = m.group(1)
    getter = session or requests
    try:
        r = getter.get(url, headers=_HEADERS, timeout=timeout,
                       allow_redirects=True)
        if r.status_code >= 400:
            return ""
    except requests.RequestException:
        return ""
    page = html.unescape(r.text)
    idx = page.find(_ARR_START)
    if idx < 0:
        return ""
    lit = _slice_array(page, idx)
    if not lit:
        return ""
    try:
        data = json.loads(_decode_js_literal(lit))
    except ValueError:
        return ""
    if not isinstance(data, list):
        return ""
    for rec in data:
        if isinstance(rec, dict) and str(rec.get("id")) == job_id:
            return _extract_text(rec.get("Job_Description") or "")
    return ""
