"""Format a stored job description into clean, structured HTML for display.

Descriptions are captured as best-effort plain text (see
scrapers/enrich/descriptions.py). They read acceptably but carry two problems:

  1. Boilerplate artifacts leak in — cookie banners, nav/share widgets
     ("Go to content", "Share on LinkedIn, opens in a new tab"), "Back to
     search results", "APPLY NOW", ATS field labels, and giant location dumps
     ("Same job available in 48 locations" + a wall of City, State rows).
  2. Structure is flat — bullets and section headings render as undifferentiated
     lines under `white-space: pre-wrap`.

This module strips the artifacts and rebuilds the text as semantic HTML
(<h4>/<ul>/<li>/<p>) so every posting looks organised regardless of which ATS
it came from. It runs at DISPLAY time, so it fixes every already-stored row
without a re-scrape and never mutates the DB. All text is HTML-escaped; only
this module's own structural tags are emitted, so the output is safe to render
unescaped.
"""
from __future__ import annotations

import re
from markupsafe import Markup, escape

# ── Artifact lines to drop outright (exact match, case-insensitive, trimmed of
# surrounding whitespace and a trailing colon). These are nav/share/apply/cookie
# chrome and ATS field labels that leak past the HTML extractor because they sit
# in plain <div>s rather than the <nav>/<footer> the extractor skips. ──────────
_DROP_EXACT = {
    # Navigation / breadcrumbs
    "go to content", "go to search", "back to offers list", "back to search results",
    "back to results", "retour", "go back", "home", "job detail", "job details",
    "skip to main content", "skip to content", "main content", "menu", "close",
    "previous", "next", "all offers", "view all jobs", "see all jobs",
    # Share widgets
    "share this page!", "share this page", "share", "share this job",
    # Apply / action chrome
    "apply", "apply now", "apply now to a top employer", "apply for this job",
    "apply for this position", "apply today", "save job", "save this job", "save",
    "print", "print this page", "email this job", "add to favorites",
    "add to favourites", "quick apply", "easy apply", "start your application",
    # Marketing chrome (BNP Paribas board furniture, etc.)
    "building the bank", "for europe's future", "read more", "show more",
    "learn more", "view more", "see more", "show less",
    "offers you may be interested in", "other corresponding job offers",
    "similar jobs", "related jobs", "recommended jobs", "you may also like",
    # Cookie / consent
    "cookies", "cookie policy", "cookie settings", "cookie preferences",
    "manage cookies", "accept all", "reject all", "accept cookies",
    "accept all cookies", "we use cookies", "necessary cookies",
    # ATS field labels that leak as standalone lines
    "general information", "job id", "job reference", "reference", "company",
    "city", "country", "location", "job type", "type of contract", "contract type",
    "job function", "functional area", "working time model", "competence line",
    "brand", "schedule", "contact", "seniority level", "employment type",
    "industry", "job category", "department", "division", "region", "posted",
    "date posted", "requisition id", "req id", "entity", "business area",
    "work experience", "education level", "travel", "salary", "compensation",
    "start date", "positions", "vacancies", "workplace", "work location",
}

# Line-level regexes to drop. Anchored to the whole (trimmed) line so they can't
# swallow a substring of real prose.
_DROP_RE = re.compile(
    r"^(?:"
    r"share on\b.*"                                        # LinkedIn/X/Facebook/… share buttons
    r"|(?:you must )?accept(?:ing)? .*cookies.*"            # "You must accept the … cookies …"
    r"|this (?:site|website|page) uses cookies.*"
    r"|we use cookies.*"
    r"|(?:manage|use|about) (?:your )?cookies.*"
    r"|.*cookies to see this content.*"
    r"|same job available in \d+ locations?.*"             # location-dump header (rows consumed separately)
    r"|\d+ (?:open )?positions?"
    r"|apply now to .*"
    r"|back to .*results.*"
    r")$",
    re.IGNORECASE,
)

# Trailing chrome: once a "similar/other jobs" section starts, everything after
# it is a list of unrelated postings — truncate the description there.
_TRAILING_MARKERS = {
    "offers you may be interested in", "other corresponding job offers",
    "similar jobs", "recommended jobs", "related jobs", "you may also like",
    "jobs you may be interested in", "more jobs at this company", "more offers",
}

# Board boilerplate paragraphs (BNP Paribas / UniCredit furniture, repeated on
# every posting). Matched as a line PREFIX (case-insensitive).
_DROP_PREFIX = (
    "ethics, at the heart of our recruitment",
    "are you a person with a disability",
    "apply to the mission handicap",
    "go to navigation", "job offer", "apply as", "last update",
    "working time model comments", "level of experience", "study level",
    "i am just starting my career", "i have significant experience",
    "i can work autonomously",
    # Study-level metadata run-ons and repeated recruiting boilerplate.
    "master degree or equivalent", "bachelor degree or equivalent",
    "we ensure that every candidate acts with integrity",
    "at bnp paribas, your skills make the difference",
    "ethics, at the heart",
)

# Metadata keywords that mark a leading ATS header line (not prose).
_META_KEYWORDS = (
    "permanent", "full time", "full-time", "part time", "part-time",
    "fixed term", "internship", "trainee", "level of experience", "study level",
    "working time model", "type of contract", "contract type", "retail banking",
    "reference", "job id", "requisition", "last update", "apply as",
    "seniority level", "employment type", "functional area", "competence line",
)
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.\w+$")
_NUMERIC_RE = re.compile(r"^[\d\s.,/()+-]+$")

# A standalone geographic line: 2-3 Title-Case, comma-separated tokens and
# nothing else (e.g. "Seville, Andalusia, Spain", "New York, United States").
# These are ATS location metadata, not prose — the location already shows in the
# detail meta grid — so a run of them (the "Same job available in N locations"
# dump) is dropped. Kept strict (no verbs, short) to avoid eating a sentence.
_GEO_LINE_RE = re.compile(
    r"^[A-Z][\w.'’\-]+(?: [A-Z][\w.'’\-]+){0,2}"
    r"(?:, [A-Z][\w.'’\-]+(?: [A-Z][\w.'’\-]+){0,2}){1,2}$"
)

# Bullet markers seen across boards (● • - * · ▪ ◦ ‣ – —, and a bare "o ").
_BULLET_RE = re.compile(r"^\s*(?:[•●▪◦‣·\-\*–—]|o(?=\s))\s+(.*\S)", re.UNICODE)

# Known section headings. Matched against the whole trimmed line (a trailing
# colon allowed). Only these plus ALL-CAPS / colon lines become <h4>, so a short
# ordinary sentence isn't mistaken for a heading.
_HEADING_ALTS = [
    r"(?:key )?(?:responsibilities|accountabilities|duties|tasks)",
    r"(?:minimum |preferred |basic |desired |key )?qualifications",
    r"requirements",
    r"your (?:profile|mission|role|responsibilities|experience|team|impact|background)",
    r"your day[ -]to[ -]day",
    r"what you(?:’|')?ll (?:do|bring|need)",
    r"what you will do",
    r"what you bring",
    r"what we offer",
    r"what we(?:’|')?re looking for",
    r"we offer",
    r"we are looking for",
    r"(?:our|the) offer",
    r"the (?:role|opportunity|team|company)",
    r"about (?:us|you|the role|the team|the company|the job|the position|the opportunity|deloitte)",
    r"who (?:we are|you are|we(?:’|')?re looking for)",
    r"(?:position|job|role) summary",
    r"job (?:description|purpose)",
    r"role purpose",
    r"overview|summary|introduction|context|mission|purpose",
    r"(?:key )?skills(?: and experience| & experience)?",
    r"experience|education",
    r"benefits|perks",
    r"why (?:join |work with )?us",
    r"how to apply",
    r"essential(?: skills| criteria| functions)?",
    r"desirable|nice to have",
    r"main (?:tasks|duties)",
    r"(?:candidate |ideal )?(?:profile|candidate)",
    r"about the program(?:me)?",
]
_HEADING_RE = re.compile(r"^(?:" + "|".join(_HEADING_ALTS) + r")\s*:?$", re.IGNORECASE)

# A short ALL-CAPS line (2+ chars, no lowercase) is a heading too — many boards
# capitalise section titles ("RESPONSIBILITIES", "YOUR EXPERIENCE").
def _is_caps_heading(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return (
        2 <= len(s) <= 48
        and len(letters) >= 2
        and not any(c.islower() for c in s)
        and not s.endswith((".", ",", ";"))
    )


def _norm(line: str) -> str:
    """Lowercase + strip a trailing colon, for blocklist matching."""
    return line.strip().rstrip(":").strip().lower()


def _ends_sentence(line: str) -> bool:
    return line.rstrip("\"'”’)]").endswith((".", "!", "?", "。"))


def _is_meta_line(line: str, low: str) -> bool:
    """A leading ATS header line (company / category / location / job-id /
    contract-type / email), as opposed to the posting's prose."""
    if _EMAIL_RE.match(line) or _NUMERIC_RE.match(line):
        return True
    if _GEO_LINE_RE.match(line):
        return True
    # Ends with a bare requisition-id number (BNP/UniCredit header run-ons do).
    if re.search(r"\b\d{5,}$", line):
        return True
    if any(k in low for k in _META_KEYWORDS):
        return True
    # A short fragment with few words and no sentence punctuation (e.g. company
    # name, "Retail Banking", a country) — only trusted inside the lead region.
    return len(line) <= 40 and line.count(" ") <= 4 and not _ends_sentence(line)


def _clean_lines(text: str, title: str | None) -> list[str]:
    """Drop artifact lines, the leading ATS metadata header, the location dump,
    board boilerplate, and any trailing 'similar jobs' section."""
    title_norm = (title or "").strip().lower()
    out: list[str] = []
    dropping_geo = False
    in_lead = True  # still inside the pre-content metadata header?
    for raw in _unescape_literals(text).split("\n"):
        line = raw.strip()
        if not line:
            out.append("")
            dropping_geo = False
            continue
        low = _norm(line)
        # Trailing "similar jobs" chrome → the rest of the page is other postings.
        if low in _TRAILING_MARKERS:
            break
        # A line that just repeats the job title ("Title - - 12345 Title").
        if title_norm and (low == title_norm or low.startswith(title_norm + " - ")):
            continue
        if low in _DROP_EXACT:
            continue
        if _DROP_RE.match(line):
            if line.lower().startswith("same job available"):
                dropping_geo = True
            continue
        if any(low.startswith(p) for p in _DROP_PREFIX):
            continue
        if dropping_geo and _GEO_LINE_RE.match(line):
            continue
        dropping_geo = False
        # Skip the leading metadata header until the first line of real prose.
        # Bullets/headings/sentence-ending lines are content and end the header;
        # otherwise a metadata-looking line is dropped (even a long run-on that
        # concatenates several ATS fields), and anything else ends the header.
        if in_lead:
            if _BULLET_RE.match(line) or _looks_heading(line) or _ends_sentence(line):
                in_lead = False
            elif _is_meta_line(line, low):
                continue
            else:
                in_lead = False
        out.append(line)
    return out


def _looks_heading(line: str) -> bool:
    if len(line) > 64:
        return False
    if _HEADING_RE.match(line):
        return True
    if line.endswith(":") and len(line) <= 64 and line.count(" ") <= 8:
        return True
    return _is_caps_heading(line)


_MULTISPACE_RE = re.compile(r"[ \t]{2,}")

# Literal JSON-style escape sequences — a backslash followed by n/r/t as TWO
# characters — leak into some stored descriptions: an ATS payload that embeds a
# JSON-encoded string inside another JSON string (double-encoding) leaves the
# inner escapes as literal text after one decode, and the row was stored
# verbatim ("Group\n\n Division"). Normalised to real whitespace here, at the
# head of the shared line cleaner, so display (format_description), the
# tagger's excerpt builder (clean_text via tag.py), and every already-stored
# row are fixed without a re-scrape.
_LITERAL_ESCAPES = (("\\r\\n", "\n"), ("\\n", "\n"), ("\\r", "\n"), ("\\t", " "))


def _unescape_literals(text: str) -> str:
    if "\\" in text:
        for seq, repl in _LITERAL_ESCAPES:
            text = text.replace(seq, repl)
    return text


def clean_text(text: str | None, title: str | None = None) -> str:
    """Plain-text sibling of format_description: run the same blocklist/heading
    cleaner but return joined text instead of HTML. Shared with tag.py's excerpt
    builder so the tagger and the display renderer strip the identical
    nav/cookie/ATS-metadata chrome from one blocklist (no duplication)."""
    if not text or not text.strip():
        return ""
    lines = _clean_lines(text, title)
    # Collapse the runs of blank markers _clean_lines emits at paragraph
    # boundaries into single newlines, and squeeze intra-line multi-space runs
    # (an ATS artifact) so the excerpt's char budget isn't wasted on padding.
    out: list[str] = []
    for line in lines:
        out.append(_MULTISPACE_RE.sub(" ", line) if line else "")
    joined = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def format_description(text: str | None, title: str | None = None) -> Markup:
    """Strip boilerplate artifacts and render the description as structured,
    escaped HTML (<h4>/<ul>/<li>/<p>). Returns an empty Markup for empty input."""
    if not text or not text.strip():
        return Markup("")

    lines = _clean_lines(text, title)

    html: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush_para():
        if para:
            html.append("<p>" + str(escape(" ".join(para))) + "</p>")
            para.clear()

    def flush_list():
        if items:
            html.append("<ul>" + "".join("<li>" + str(escape(i)) + "</li>" for i in items) + "</ul>")
            items.clear()

    for line in lines:
        if not line:                      # blank line → paragraph/list boundary
            flush_list()
            flush_para()
            continue
        line = _MULTISPACE_RE.sub(" ", line)
        m = _BULLET_RE.match(line)
        if m:
            flush_para()
            items.append(m.group(1).strip())
            continue
        # non-bullet line
        flush_list()
        if _looks_heading(line):
            flush_para()
            html.append("<h4>" + str(escape(line.rstrip(":").strip())) + "</h4>")
        else:
            para.append(line)
    flush_list()
    flush_para()

    return Markup("".join(html)) if html else Markup("")
