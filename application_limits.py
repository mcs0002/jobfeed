"""
Company-level application caps — "you may apply to at most N roles per
recruitment year" — researched from each firm's own early-careers pages.

WHY THIS EXISTS
---------------
the user favourites the same graduate programme in several locations (the UBS
Global Markets programme was starred in nine at the time this was written). If
a firm caps applications per cycle, the choice of *which* location to send is
made the moment the first form is submitted, and it is unrecoverable. The cap
therefore has to be known before the first application, not discovered after.

ACCURACY CONTRACT
-----------------
This module is allowed to be wrong by saying "unknown". It is not allowed to be
wrong by inventing a number. Three gates enforce that, in order:

1. A number is only ever read out of text this process actually fetched over
   the network in this run. Nothing comes from model knowledge — the prompt
   carries the page text and the model is told to copy, not recall.
2. The model must return a verbatim `quote`. That quote is checked as a
   substring of the fetched page text (whitespace-normalised, case-folded).
   A quote that is not in the page means the model paraphrased or hallucinated,
   and the whole extraction is discarded.
3. The digit or number-word for `max_per_cycle` must itself appear in the
   quote. This catches the residual case where the model lifts a real sentence
   but attaches the wrong number to it.

A discarded extraction is stored as confidence='unknown' with the URLs we
checked, so the next pass knows the ground was already covered and the user
knows the silence is measured rather than merely absent.

Usage:
    python application_limits.py                     # every configured firm
    python application_limits.py --company Barclays  # one firm
    python application_limits.py --company "UBS (Graduate Careers)" \
        --url https://www.ubs.com/global/en/careers/early-careers/faq.html
    python application_limits.py --refresh-days 90 --limit 50
    LIMITS_BROWSERS=5 python application_limits.py --refresh-days 0

Firms are taken from targets.json: `limits_url` if present, else
`grad_scheme_url`, else `career_url` under --all.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from db import JobDB, quote_states_number               # noqa: E402
from scrapers.enrich.descriptions import _extract_text  # noqa: E402
import tag as _tag                                      # noqa: E402

DB_FILE = os.path.join(ROOT, "jobs.db")
TARGETS = os.path.join(ROOT, "targets.json")
LOG_PATH = os.path.join(ROOT, "application_limits.log")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
MAX_PAGES_PER_FIRM = 6
FETCH_WORKERS = 8

# ── Gate 1: the prefilter ────────────────────────────────────────────────────
# Cheap, deterministic, and it decides whether the LLM is called at all. Its
# job is RECALL, not precision — a false positive costs one small API call, a
# false negative loses a real cap silently.
#
# Calibrated against Deutsche Bank's graduate FAQ (2026-09-08), which states
# its cap as "...then apply to just one role in one country." An earlier
# hand-built alternation of "maximum of N applications" phrasings scored zero
# hits on that page: firms almost never use the words you expect. So the test
# is compositional instead — a sentence fires when it mentions APPLYING, a
# RESTRICTION, and a SMALL QUANTITY together. FAQ question headings ("Can I
# apply to more than one division?") survive as their own sentences and fire on
# their own, with the answer pulled in by the surrounding window.
_APPL = r"appl(?:y|ies|ying|ication|ications)"
_RESTRICT = (r"\b(?:only|just|more than|maximum|max\.?|no more than|at most|"
             r"limited|limit|limits|restrict\w*|cannot|can't|can not|may not|"
             r"unable|prohibit\w*|not permitted|permitted|allowed|one at a time|"
             r"simultaneous\w*|concurrent\w*|withdraw\w*)\b")
_QUANTITY = r"\b(?:one|two|three|four|five|1|2|3|4|5|single|multiple|several)\b"
# What a cap is counted in. "Yes, you may apply to one office and one division
# per year" (Bank of America) carries no restriction word at all — the limit is
# expressed purely as a quantity of scopes — so applying + quantity + scope has
# to fire on its own. It is a loose test that will pass some innocent sentences;
# that costs one cheap model call each, while missing this one costs a cap.
_SCOPE = (r"\b(?:office|offices|division|divisions|programme|programmes|"
          r"program|programs|role|roles|position|positions|location|locations|"
          r"country|countries|region|regions|scheme|schemes|stream|streams|"
          r"opportunity|opportunities|vacancy|vacancies|job|jobs|"
          r"application|applications|business area|business areas)\b")

_APPL_RE = re.compile(_APPL, re.IGNORECASE)
_RESTRICT_RE = re.compile(_RESTRICT, re.IGNORECASE)
_QUANTITY_RE = re.compile(_QUANTITY, re.IGNORECASE)
_SCOPE_RE = re.compile(_SCOPE, re.IGNORECASE)
# Phrases strong enough to fire without the full triple.
_STRONG_RE = re.compile(
    r"application\s+(?:limit|cap|restriction|policy|quota)"
    r"|number\s+of\s+applications"
    r"|how\s+many\s+(?:applications|roles|positions|programmes|programs)"
    r"|one\s+application\s+per"
    r"|per\s+(?:recruitment\s+)?(?:cycle|season|campaign|intake)",
    re.IGNORECASE)

# Sentence split that also treats line breaks and FAQ accordion headings as
# boundaries — careers pages are mostly fragments, not prose.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def sentence_is_capish(sentence: str) -> bool:
    """True if a sentence is worth showing the model. See the note above."""
    if len(sentence) > 600:
        return False
    if _STRONG_RE.search(sentence):
        return True
    if not (_APPL_RE.search(sentence) and _QUANTITY_RE.search(sentence)):
        return False
    return bool(_RESTRICT_RE.search(sentence) or _SCOPE_RE.search(sentence))


# Links worth following from a programme landing page.
LINK_HINTS = (
    "faq", "frequently-asked", "frequently_asked", "questions",
    "application-process", "application_process", "applicationprocess",
    "how-to-apply", "howtoapply", "how_to_apply", "applying",
    "recruitment-process", "hiring-process", "selection-process",
    "our-process", "eligibility", "requirements", "apply",
)
# Never worth following — legal boilerplate that matches "apply" and burns a slot.
LINK_BLOCK = (
    "privacy", "cookie", "terms", "accessibility", "legal", "gdpr",
    "modern-slavery", "sitemap", "login", "signin", "sign-in", "register",
)


_print_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    with _print_lock:
        print(line, flush=True)
        try:
            with open(LOG_PATH, "a") as fp:
                fp.write(line + "\n")
        except OSError:
            pass


# ── Fetching ─────────────────────────────────────────────────────────────────
class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href, self._text = None, []


def _same_site(base: str, url: str) -> bool:
    """Same registrable-ish domain. Career sites hop between careers.db.com and
    db.com constantly, so netloc equality is too strict; last two labels is the
    pragmatic middle ground (it lets through db.com <-> careers.db.com but not
    db.com -> linkedin.com)."""
    def key(u):
        host = (urlparse(u).hostname or "").lower()
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    return key(base) == key(url)


# A bare User-Agent gets 403'd by the WAFs in front of several careers sites
# (BNP Paribas and Citadel both refused the first pass). A full, ordinary
# browser header set is what those filters are actually looking for.
HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-GB,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def http_get(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                        allow_redirects=True)
    if resp.status_code in (403, 429):
        # One polite retry — some WAFs pass a second request on a warm
        # connection that they refuse cold.
        time.sleep(1.5)
        resp = requests.get(url, headers={**HEADERS, "Referer": url},
                            timeout=TIMEOUT, allow_redirects=True)
    return resp


# Playwright is optional and slow, so it is a fallback rather than the default:
# it only runs when plain HTTP is refused or returns a page with no readable
# text. UBS — the firm with the most starred roles by a wide margin — answers
# 403 to any requests call regardless of headers, and its early-careers FAQ is
# where its regional application caps are written down. A tool that silently
# gives up there fails at exactly the case it was built for.
# A semaphore rather than a lock: a few concurrent Chromiums keep a 300-firm
# sweep to a sensible wall-clock time without swamping the M1, which also hosts
# the scraper and the web app. Two is the safe default; LIMITS_BROWSERS raises
# it for a full sweep, which is otherwise render-bound at roughly 1.5 firms a
# minute and would still be running when the 04:00 scan starts.
_BROWSER_SLOTS = threading.Semaphore(int(os.environ.get("LIMITS_BROWSERS", "2")))
# Rendering is bounded per firm as well. Once plain HTTP has come up empty, the
# cap — if it exists — is overwhelmingly on the landing page or the top-ranked
# FAQ candidate; rendering all ten probed paths would multiply the cost of the
# common (genuinely no cap) case by an order of magnitude for almost no recall.
MAX_RENDER_PAGES = 2


def fetch_rendered(url: str) -> tuple[str, str | None]:
    """Fetch through a headless browser. Serialised: a Chromium per worker
    would swamp the M1, and this path is rare by construction."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "", "playwright not installed"
    try:
        with _BROWSER_SLOTS, sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=UA, viewport={"width": 1280, "height": 900},
                    locale="en-GB")
                page = context.new_page()
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                # Careers FAQs are usually accordions rendered after load.
                page.wait_for_timeout(3500)
                html = page.content()
            finally:
                browser.close()
        return _extract_text(html, max_chars=60000), None
    except Exception as exc:
        return "", f"render failed: {type(exc).__name__}: {exc}"


def fetch(url: str, allow_render: bool = True) -> tuple[str, str | None]:
    """(text, error). Never raises — a dead careers site is data, not a crash."""
    error = None
    # A 404 from a guessed path means the page does not exist, so there is
    # nothing for a browser to render. Without this, every miss in the
    # conventional-path probe list (most of them, by design) spawned a Chromium
    # page load and the sweep crawled at ~1.6 firms a minute.
    absent = False
    try:
        resp = http_get(url)
        if resp.status_code >= 400:
            error = f"HTTP {resp.status_code}"
            absent = resp.status_code in (404, 410)
        else:
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                return "", f"content-type {ctype!r}"
            text = _extract_text(resp.text, max_chars=60000)
            # A 200 with almost no text is a client-rendered shell, not a page.
            if len(text) >= 400:
                return text, None
            error = f"only {len(text)} chars of text (client-rendered?)"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    if allow_render and not absent:
        text, render_error = fetch_rendered(url)
        if text:
            return text, None
        error = f"{error}; {render_error}"
    return "", error


# Conventional FAQ/process paths, tried against both the landing page's own
# directory and the site root.
PROBE_SUFFIXES = ("faq", "faqs", "faq.html", "faqs.html", "application-process",
                  "how-to-apply", "applying", "application-faqs",
                  "recruitment-process", "hiring-process", "process")


def conventional_paths(landing: str) -> list[str]:
    parsed = urlparse(landing)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    directory = parsed.path.rstrip("/")
    bases = []
    if directory:
        bases.append(f"{origin}{directory}")
        parent = directory.rsplit("/", 1)[0]
        if parent:
            bases.append(f"{origin}{parent}")
    bases.append(origin)
    out, seen = [], set()
    for base in bases:
        for suffix in PROBE_SUFFIXES:
            url = f"{base}/{suffix}"
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


def fetch_firm_pages(landing: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Fetch the landing page plus the most promising FAQ/process subpages.

    Returns ([(url, text), ...], errors). The landing page is always first so a
    firm that states its cap up front is resolved in one request."""
    pages: list[tuple[str, str]] = []
    errors: list[str] = []
    html, final_url = "", landing
    try:
        resp = http_get(landing)
        resp.raise_for_status()
        html, final_url = resp.text, resp.url
    except Exception as exc:
        errors.append(f"{landing}: {type(exc).__name__}: {exc}")

    landing_text = _extract_text(html, max_chars=60000) if html else ""
    if len(landing_text) < 400:
        # No usable text from plain HTTP — fall back to a real browser before
        # writing the firm off. There are no links to follow either way, so the
        # conventional-path probes below carry the rest.
        rendered, render_error = fetch_rendered(landing)
        if rendered:
            landing_text = rendered
        elif not html:
            return [], errors + [f"{landing}: {render_error}"]
    if landing_text:
        pages.append((final_url, landing_text))

    parser = _Links()
    try:
        parser.feed(html)
    except Exception:
        pass

    scored: list[tuple[int, str]] = []
    seen = {final_url.rstrip("/")}
    for href, text in parser.links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(final_url, href)
        if not absolute.startswith("http") or not _same_site(final_url, absolute):
            continue
        key = absolute.split("#")[0].rstrip("/")
        if key in seen:
            continue
        blob = (key + " " + text).lower()
        if any(b in blob for b in LINK_BLOCK):
            continue
        score = sum(2 if hint in key.lower() else 1
                    for hint in LINK_HINTS if hint in blob)
        # A page that already advertises itself as an FAQ is the single most
        # likely home of the cap sentence — weight it above generic "apply".
        if "faq" in blob or "frequently" in blob:
            score += 4
        if score:
            seen.add(key)
            scored.append((score, absolute))

    scored.sort(key=lambda s: -s[0])
    candidates = [u for _s, u in scored[:MAX_PAGES_PER_FIRM - 1]]

    # Most big careers sites render their nav in JavaScript, so the raw HTML we
    # can parse contains no FAQ link at all — Goldman's students page offers
    # exactly one scoring link out of 114. Guessing conventional paths costs a
    # HEAD-ish GET each and is empirical, not invention: the URL either returns
    # a real page or it doesn't, and anything it yields still has to survive
    # all three extraction gates.
    if len(candidates) < MAX_PAGES_PER_FIRM - 1:
        for probe in conventional_paths(final_url):
            if probe.rstrip("/") in seen:
                continue
            seen.add(probe.rstrip("/"))
            candidates.append(probe)
            if len(candidates) >= MAX_PAGES_PER_FIRM + 3:
                break

    for url in candidates:
        text, err = fetch(url)
        if err:
            errors.append(f"{url}: {err}")
        elif text:
            pages.append((url, text))
    return pages, errors


# ── Gate 2 + 3: extraction and verification ──────────────────────────────────
_NORM_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _NORM_WS.sub(" ", (s or "")).strip().lower()


def cap_windows(text: str, radius: int = 700) -> list[str]:
    """Context windows around every prefilter hit, merged when they overlap.

    The window matters as much as the hit: on an FAQ page the trigger is often
    the question ("Can I apply to more than one division?") while the number
    lives in the answer two lines below, so a hit is expanded generously in
    both directions before the model ever sees it."""
    # Exact offsets: derive sentence spans from the DELIMITER spans rather than
    # from split() + find(). A find()-based walk desynchronises the moment one
    # sentence is not located from the running cursor, and every window after
    # it lands in the wrong part of the page — which is how the Deutsche Bank
    # answer got truncated mid-clause on the first attempt.
    spans: list[tuple[int, int]] = []
    bounds: list[tuple[int, int]] = []
    prev = 0
    for m in _SENT_SPLIT.finditer(text):
        bounds.append((prev, m.start()))
        prev = m.end()
    bounds.append((prev, len(text)))
    for lo, hi in bounds:
        sentence = text[lo:hi]
        if sentence.strip() and sentence_is_capish(sentence):
            spans.append((max(0, lo - radius), min(len(text), hi + radius)))
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return [text[lo:hi] for lo, hi in merged[:6]]


def windows_or_render(pages: list[tuple[str, str]]) -> tuple[list[str], list[tuple[str, str]]]:
    """Cap windows across all pages, re-fetching through a browser first if
    plain HTTP found nothing.

    A 200 with plenty of text is not proof the page is complete: Bank of
    America's process page returns ~5k characters over plain HTTP, and the
    sentence that matters — "Yes, you may apply to one office and one division
    per year" — lives in an accordion that only exists after JavaScript runs.
    The length check in fetch() cannot catch that, so the trigger for rendering
    is a NEGATIVE RESULT rather than a short one: we only conclude "no cap
    stated" after a real browser has also looked."""
    windows: list[str] = []
    for _u, text in pages:
        windows.extend(cap_windows(text))
    if windows:
        return windows, pages

    upgraded: list[tuple[str, str]] = []
    for i, (url, text) in enumerate(pages):
        if i >= MAX_RENDER_PAGES:
            upgraded.append((url, text))
            continue
        rendered, _err = fetch_rendered(url)
        if rendered and len(rendered) > len(text):
            upgraded.append((url, rendered))
            windows.extend(cap_windows(rendered))
        else:
            upgraded.append((url, text))
    return windows, upgraded


SYSTEM = """You extract application-limit policies from recruiting web pages.

You will be given text COPIED FROM A COMPANY'S OWN CAREERS PAGE. Answer ONLY
from that text. You must never use anything you know about the company from
memory — if the text does not state a limit, the answer is that there is no
limit stated.

Return a single JSON object, no prose, no markdown fence:

{"has_limit": bool,
 "max_per_cycle": int or null,
 "cycle": "the period the limit applies to, in the page's own words, or \\"\\"",
 "locations_count_separately": true | false | null,
 "shared_across_programmes": true | false | null,
 "strength": "hard" | "advisory",
 "varies_by_region": true | false,
 "quote": "one sentence copied EXACTLY from the supplied text"}

Rules:
- has_limit is true ONLY for a cap on how many applications/roles a candidate
  may submit. A deadline, a number of available places, a number of interview
  rounds, a cohort size, or "apply early" advice is NOT an application limit.
- "quote" must be copied character-for-character from the supplied text and
  must contain the number. If you cannot produce such a quote, set has_limit
  false. A paraphrase is a failure, not an approximation.
- strength: "hard" when the firm states a rule it enforces ("we only accept
  one application per person", "you can only apply to one programme");
  "advisory" when it is guidance ("we recommend you focus on no more than
  three"). This distinction changes what he is allowed to do, so do not
  smooth it over.
- varies_by_region: true when the page gives DIFFERENT allowances for
  different regions or countries. When it is true, put max_per_cycle at the
  SMALLEST of the stated allowances and make the quote cover the sentences
  that establish the variation, copied contiguously from the text. A single
  number is not the answer in that case and must not be presented as one.
- locations_count_separately: true if applying to the same programme in two
  cities uses two of the allowance; false if the page says location choices
  are made within one application; null if not addressed.
- shared_across_programmes: true if internship and graduate applications draw
  on the same allowance; null if not addressed.
- If the text is ambiguous, prefer has_limit false."""


def call_model(company: str, windows: list[str]) -> dict | None:
    """One extraction call through the same OpenAI-compatible transport the
    tagger uses (DeepSeek in production). Returns the parsed object or None."""
    cfg = _tag._openai_cfg()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        raise RuntimeError(
            "No OpenAI-compatible transport configured (TAG_API_BASE_URL / "
            "TAG_API_KEY / TAG_API_MODEL). This tool refuses to guess.")
    body = "\n\n---\n\n".join(w.strip() for w in windows)[:12000]
    user = f"Company: {company}\n\nPAGE TEXT:\n\n{body}"
    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}",
                     "content-type": "application/json"},
            json={"model": cfg["model"], "max_tokens": 700,
                  **cfg.get("extra", {}),
                  "messages": [{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": user}],
                  "temperature": 0, "stream": False},
            timeout=90)
        resp.raise_for_status()
        text = ((resp.json().get("choices") or [{}])[0]
                .get("message", {}) or {}).get("content") or ""
    except Exception as exc:
        log(f"  ! model call failed for {company}: {type(exc).__name__}: {exc}")
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def verify(parsed: dict, pages: list[tuple[str, str]]) -> tuple[dict | None, str]:
    """Gates 2 and 3. Returns (accepted_record, reason_if_rejected)."""
    if not parsed or not parsed.get("has_limit"):
        return None, "no limit stated"

    n = parsed.get("max_per_cycle")
    if not isinstance(n, int) or not (1 <= n <= 20):
        return None, f"implausible max_per_cycle {n!r}"

    quote = (parsed.get("quote") or "").strip()
    if len(quote) < 15:
        return None, "quote too short to verify"

    # Gate 2: the quote must exist in a page we fetched.
    nq = _norm(quote)
    source_url = ""
    for url, text in pages:
        if nq in _norm(text):
            source_url = url
            break
    if not source_url:
        return None, "QUOTE NOT FOUND IN FETCHED TEXT (discarded)"

    # Gate 3: the number must be in the quote it came from.
    if not quote_states_number(n, nq):
        return None, f"number {n} absent from its own quote (discarded)"

    def tri(v):
        return None if v is None else (1 if v else 0)

    strength = (parsed.get("strength") or "").strip().lower()
    return {
        "max_per_cycle": n,
        "varies_by_region": tri(parsed.get("varies_by_region")),
        "strength": strength if strength in ("hard", "advisory") else "",
        "cycle": (parsed.get("cycle") or "")[:120],
        "locations_count_separately": tri(parsed.get("locations_count_separately")),
        "shared_across_programmes": tri(parsed.get("shared_across_programmes")),
        "quote": quote[:600],
        "source_url": source_url,
    }, ""


# ── Driver ───────────────────────────────────────────────────────────────────
def research_urls(company: str, urls: list[str]) -> dict:
    """Research a company from URLs supplied on the command line.

    The automatic crawler cannot reach every firm — a JS-rendered careers site
    exposes no links to follow, and some firms (UBS, Nomura, Bank of America)
    have no early-careers landing page configured at all. This is the manual
    escape hatch, and it is deliberately NOT a weaker one: the pages are
    fetched here and now, and the extraction passes exactly the same three
    gates as an automated run."""
    pages: list[tuple[str, str]] = []
    errors: list[str] = []
    for url in urls:
        text, err = fetch(url)
        if err:
            errors.append(f"{url}: {err}")
        elif text:
            pages.append((url, text))
            log(f"  fetched {len(text)} chars from {url}")
        else:
            errors.append(f"{url}: empty after text extraction")
    if not pages:
        return {"company": company, "confidence": "fetch_failed",
                "max_per_cycle": None, "source_url": urls[0] if urls else "",
                "quote": "; ".join(errors)[:600]}
    windows, pages = windows_or_render(pages)
    log(f"  {len(pages)} page(s), {len(windows)} cap-pattern window(s)")
    if not windows:
        return {"company": company, "confidence": "unknown", "max_per_cycle": None,
                "source_url": pages[0][0],
                "quote": f"checked {len(pages)} page(s), no cap language found"}
    record, reason = verify(call_model(company, windows), pages)
    if record is None:
        log(f"  rejected — {reason}")
        return {"company": company, "confidence": "unknown", "max_per_cycle": None,
                "source_url": pages[0][0],
                "quote": f"checked {len(pages)} page(s); {reason}"}
    record.update({"company": company, "confidence": "stated"})
    log(f"  ✓ {company}: {record['max_per_cycle']} per "
        f"{record['cycle'] or 'cycle'} [{record['strength'] or 'unspecified'}]"
        f" — {record['source_url']}")
    return record


def load_targets(only: str | None, include_all: bool) -> list[dict]:
    with open(TARGETS) as fp:
        targets = json.load(fp)
    out = []
    for t in targets:
        # `limits_url` overrides, for two reasons. Some firms have no
        # grad_scheme_url at all (UBS, Nomura and Bank of America between them
        # hold 16 of the 47 starred roles), and for others the page that
        # actually states the cap is not the programme landing page — Barclays
        # publishes its "one role globally each year" line on /faqs, not on
        # /early-careers. It is a separate field rather than a backfill of
        # grad_scheme_url because grad_scheme carries its own meaning here:
        # per GRAD_SCHEMES.md it marks firms whose roles the ATS scrape MISSES
        # and which must be applied to directly, which is not true of these.
        url = (t.get("limits_url") or t.get("grad_scheme_url")
               or (t.get("career_url") if include_all else None))
        if not url:
            continue
        if only and only.lower() not in t["name"].lower():
            continue
        out.append({"name": t["name"], "url": url,
                    "category": t.get("category", "")})
    return out


def research_one(firm: dict, verbose: bool = False) -> dict:
    """Fetch → prefilter → extract → verify for one firm. Pure: returns the
    record to write, never touches the DB (the caller owns the connection)."""
    name, url = firm["name"], firm["url"]
    pages, errors = fetch_firm_pages(url)
    if not pages:
        return {"company": name, "confidence": "fetch_failed",
                "source_url": url, "quote": "; ".join(errors)[:600],
                "max_per_cycle": None, "note": "fetch failed"}

    windows, pages = windows_or_render(pages)
    if verbose:
        log(f"  {name}: {len(pages)} pages, {len(windows)} cap-pattern windows")
    if not windows:
        return {"company": name, "confidence": "unknown", "max_per_cycle": None,
                "source_url": url,
                "quote": f"checked {len(pages)} page(s), no cap language found",
                "note": "no prefilter hit"}

    parsed = call_model(name, windows)
    record, reason = verify(parsed, pages)
    if record is None:
        if verbose or "discarded" in reason:
            log(f"  {name}: rejected — {reason}")
        return {"company": name, "confidence": "unknown", "max_per_cycle": None,
                "source_url": url,
                "quote": f"checked {len(pages)} page(s); {reason}",
                "note": reason}
    record.update({"company": name, "confidence": "stated"})
    log(f"  ✓ {name}: {record['max_per_cycle']} per {record['cycle'] or 'cycle'}"
        f" [{record['strength'] or 'unspecified'}"
        f"{'; VARIES BY REGION' if record.get('varies_by_region') else ''}]"
        f" — {record['source_url']}")
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--company", help="substring match, one firm")
    ap.add_argument("--limit", type=int, help="stop after N firms")
    ap.add_argument("--refresh-days", type=int, default=180,
                    help="skip firms checked within this many days (default 180)")
    ap.add_argument("--all", action="store_true",
                    help="also firms with only a career_url (no grad scheme)")
    ap.add_argument("--workers", type=int, default=FETCH_WORKERS)
    ap.add_argument("--dry-run", action="store_true", help="do not write to the DB")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--url", action="append", default=[],
                    help="research these URLs for --company instead of "
                         "crawling (repeatable)")
    args = ap.parse_args()

    if args.url:
        if not args.company:
            ap.error("--url requires --company (the exact company name)")
        db = JobDB(DB_FILE)
        rec = research_urls(args.company, args.url)
        if not args.dry_run:
            db.set_company_limit(
                rec["company"], max_per_cycle=rec.get("max_per_cycle"),
                cycle=rec.get("cycle", ""),
                locations_count_separately=rec.get("locations_count_separately"),
                shared_across_programmes=rec.get("shared_across_programmes"),
                varies_by_region=rec.get("varies_by_region"),
                confidence=rec["confidence"], strength=rec.get("strength", ""),
                quote=rec.get("quote", ""), source_url=rec.get("source_url", ""),
                updated_by="research")
        return 0

    firms = load_targets(args.company, args.all)
    db = JobDB(DB_FILE, check_same_thread=False)
    if args.refresh_days:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=args.refresh_days)).isoformat()
        done = db.limits_checked_since(cutoff)
        before = len(firms)
        firms = [f for f in firms if f["name"] not in done]
        if before != len(firms):
            log(f"skipping {before - len(firms)} firm(s) checked in the last "
                f"{args.refresh_days} days")
    if args.limit:
        firms = firms[:args.limit]

    log(f"researching application limits for {len(firms)} firm(s)")
    found = unknown = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(research_one, f, args.verbose): f for f in firms}
        for i, fut in enumerate(as_completed(futures), 1):
            firm = futures[fut]
            try:
                rec = fut.result()
            except Exception as exc:
                log(f"  ! {firm['name']} crashed: {type(exc).__name__}: {exc}")
                continue
            if rec["confidence"] == "stated":
                found += 1
            elif rec["confidence"] == "fetch_failed":
                failed += 1
            else:
                unknown += 1
            if not args.dry_run:
                db.set_company_limit(
                    rec["company"], max_per_cycle=rec.get("max_per_cycle"),
                    cycle=rec.get("cycle", ""),
                    locations_count_separately=rec.get("locations_count_separately"),
                    shared_across_programmes=rec.get("shared_across_programmes"),
                    varies_by_region=rec.get("varies_by_region"),
                    confidence=rec["confidence"], strength=rec.get("strength", ""),
                    quote=rec.get("quote", ""),
                    source_url=rec.get("source_url", ""), updated_by="research")
            if i % 25 == 0:
                log(f"  … {i}/{len(firms)} (found {found}, unknown {unknown}, "
                    f"fetch-failed {failed})")

    log(f"done: {found} stated, {unknown} unknown, {failed} fetch-failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
