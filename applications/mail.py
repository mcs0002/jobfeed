"""Move a role's CRM status from what the inbox says, with the evidence kept.

The website is the tracking surface, but a status only changed when the user
remembered to change it: on 2026-09-12 the live board held 56,346 `new` rows
against 3 `applied` and 1 `rejected`, while the inbox held confirmations for
both applications sent that day. This reads those mails and moves the row.

Two rules carry the whole design, both borrowed from `applications/limits.py`:

1. **Match uniquely or do not match.** A misattributed rejection is the worst
   outcome here, because it makes him stop chasing something still live. The
   candidate pool is only the roles he has acted on, never the whole board, and
   exactly one candidate must survive or the mail is queued for him instead.
2. **Quote, never summarise.** Every recorded transition carries the sentence
   that caused it, copied out of a message fetched in that run, so a wrong call
   is visible rather than plausible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jobfeed.db import APPLICATION_MAIL_ORDER, JobDB, mail_rank, plain_company

ROOT = Path(__file__).resolve().parent.parent
# jobfeed/notify.py's subject prefix. Kept literal rather than imported: notify pulls in
# SMTP configuration, and this module must stay importable without it.
SELF_ALERT_PREFIX = "[job-scraper]"
# A job board advertises; it never corresponds about an application, because he
# applies on the employer's own ATS page and never through one of these. On
# 2026-09-17 a LinkedIn alert headed "Max the user, bewerben Sie sich jetzt bei
# Santander Corporate" carried a firm token and German recruiting language, so
# both gates passed, and it was hung on his Santander programme with the advert's
# own call-to-action button, "Jetzt bewerben", stored as a task he had to do.
AGGREGATOR_SENDERS = (
    "linkedin.com", "indeed.com", "stepstone.de", "stepstone.com", "xing.com",
    "glassdoor.com", "monster.de", "monster.com", "ziprecruiter.com",
    "talent.com", "totaljobs.com", "efinancialcareers.com",
)
# The ATS mails this when a run saves a draft, so it is this system's own
# footprint rather than news from the employer. Aurora's "Resume Your Job
# Application" on 2026-09-17 said "You can continue your application for 2027
# Graduate Analyst Program (Austin) by clicking the button below", which the
# model read as a next-stage invitation and proposed as `oa` -- promoting the
# role to online assessment hours before it was even submitted.
_DRAFT_RESUME_RE = re.compile(
    r"\b(?:resume|continue|complete|finish)\s+(?:your\s+)?"
    r"(?:saved\s+|draft\s+|job\s+)?application\b"
    r"|\bapplication\s+(?:was\s+)?saved\b"
    r"|\bbewerbung\s+fortsetzen\b", re.IGNORECASE)
MAILBOX = Path.home() / ".local" / "bin" / "mailbox"

# Everything he has acted on. `new` and `ignored` were never applied to, and a
# `rejected` role is closed: leaving them out keeps the pool at a handful.
# `rejected` is here for MATCHING, not for moving: a rejection mail must still
# find its role when it is reprocessed, and is_forward already refuses to move
# anything backwards. Leaving it out meant bp's two rejections stopped matching
# themselves the moment they had been applied.
CANDIDATE_STATUSES = APPLICATION_MAIL_ORDER

# Ordered: the first match wins, so a rejection is read as a rejection even when
# the mail also thanks him for applying. German included, since a good half of
# these arrive in German.
CLASSIFIERS: tuple[tuple[str, str], ...] = (
    ("rejected", r"(?:we\s+(?:regret|are\s+sorry)\s+to\s+(?:inform|advise|tell)"
                 r"|do(?:es)?\s+not\s+meet\s+the\s+(?:essential|minimum|required)"
                 r"|have\s+not\s+been\s+shortlisted"
                 r"|will\s+not\s+be\s+taking\s+your\s+application"
                 r"|not\s+(?:be\s+)?(?:moving|progressing|proceeding|taking)\s+(?:you\s+)?forward"
                 r"|not\s+been\s+(?:selected|successful)"
                 r"|will\s+not\s+be\s+progressing"
                 r"|decided\s+to\s+(?:move|proceed)\s+forward\s+with\s+other"
                 r"|unable\s+to\s+offer\s+you"
                 # Flow Traders, 2026-09-17: read as a confirmation by the
                 # "thank you for your interest" that opens it.
                 r"|(?:will\s+)?not\s+be\s+able\s+to\s+continue\s+with\s+your\s+(?:candidacy|application)"
                 r"|did\s+not\s+meet\s+our\s+(?:\w+\s+){0,2}(?:standards?|requirements|criteria)"
                 r"|leider\s+(?:absagen|eine\s+absage|keine\s+zusage)"
                 r"|leider\s+nicht\s+(?:weiter|ber(?:ü|u)cksichtig\w*|"
                 r"zu\s+einem|in\s+die\s+engere)"
                 r"|eine\s+absage\s+erteilen"
                 r"|nicht\s+weiter\s+ber(?:ü|u)cksichtigen)"),
    ("offer", r"(?:pleased\s+to\s+offer|offer\s+of\s+employment|formal\s+offer"
              r"|freuen\s+uns,?\s+ihnen\s+ein\s+angebot)"),
    ("interview", r"(?:invite\s+you\s+to\s+(?:an?\s+)?(?:interview|first\s+round)"
                  r"|schedule\s+(?:an?\s+)?(?:interview|call)"
                  r"|interview\s+invitation"
                  r"|einladung\s+zu\s+einem\s+(?:interview|gespr(?:ä|a)ch))"),
    ("oa", r"(?:online\s+assessment|hackerrank|codility|codesignal|karat"
           r"|numerical\s+reasoning|aptitude\s+test|timed\s+test"
           r"|online[- ]test|eignungstest)"),
    ("applied", r"(?:thank\s+you\s+for\s+(?:applying|your\s+application"
                r"|your\s+interest\s+in)"
                r"|(?:successful\s+submission|we(?:'ve| have)\s+received)\s+"
                r"(?:of\s+)?(?:your\s+)?(?:job\s+)?application"
                r"|application\s+(?:has\s+been\s+)?received"
                r"|vielen\s+dank\s+f(?:ü|u)r\s+(?:ihre\s+bewerbung|ihr\s+interesse"
                r"|deine\s+bewerbung))"),
)

# Mail that asks him to do something, whether or not it moves the CRM status.
# BlackRock's "Action required: complete your pre-interview assessment" changes
# no status and is the single most expensive message to miss.
_ACTION_RE = re.compile(
    r"(?:action\s+required|you\s+are\s+invited\s+to\s+complete|please\s+complete"
    r"|complete\s+your\s+(?:application|assessment|profile|video)"
    r"|next\s+step[s]?\s*(?::|is|are)|book\s+(?:a|your)\s+(?:slot|time|interview)"
    r"|schedule\s+your|submit\s+your\s+(?:assessment|documents|video)"
    r"|expires?\s+(?:on|in)\b|within\s+\d+\s+(?:days|hours)"
    r"|by\s+\d{1,2}\s+\w+\s+20\d\d"
    r"|bitte\s+(?:vervollst(?:ä|a)ndigen|schlie(?:ß|ss)en\s+sie)"
    r"|handlungsbedarf)", re.IGNORECASE)

_WORD_RE = re.compile(r"[a-z0-9]+")
# Nothing is classified without one of these. A dry run over 144 real messages
# read "leider nicht rechtzeitig" in a car-registration mail as a rejection;
# recruiting language is what separates a hiring mail from the rest of an inbox.
_CONTEXT_RE = re.compile(
    r"\b(?:appl(?:y|ying|ication|icant)|candidat\w*|recruit\w*|hiring|"
    r"vacanc\w*|position|role|interview|assessment|talent\s+acquisition|"
    r"bewerb\w*|stellen(?:angebot|ausschreibung|anzeige)\w*|karriere|"
    r"personal(?:abteilung|referent\w*|wesen))\b", re.IGNORECASE)
# German compounds are why these are spelled out rather than stemmed: "stelle\w*"
# matched the verb "Stellen" and "personal\w*" matches "Personalausweis", both of
# which turn a car-registration mail into a hiring mail.
# Words that identify nobody: matching on these would pair any bank with any mail.
_STOPWORDS = frozenset({
    "the", "and", "group", "inc", "llc", "ltd", "plc", "sa", "ag", "nv", "bv",
    "co", "corp", "company", "holdings", "international", "global", "partners",
    "capital", "markets", "bank", "banking", "securities", "management",
    "asset", "investment", "investments", "financial", "finance", "services",
    "trading", "graduate", "careers", "career", "gmbh", "se", "spa",
    # Words that name this whole industry rather than one firm in it. Shell
    # Trading & Supply reduced to shell plus supply, and its own confirmation
    # mail never says supply, so the firm went unmatched.
    "supply", "shipping", "energy", "commodities", "holding", "programme",
    "program", "early",
})

# A requisition id is the one thing in a careers mail that names a single role.
# They come as RQ115652, R205152, JR102789-1, R-018524: a short alphabetic
# prefix and digits, matched as a substring because the mail writes RQ115652
# with no word boundary inside it.
# A letter prefix is required, or six digits and up. Allowing bare four-digit
# runs made "2027" in a job id a requisition id, and it then matched every mail
# that mentioned the year.
_REQ_RE = re.compile(r"\b(?:[A-Z]{1,3}-?\d{4,}(?:-\d+)?|\d{6,})\b")


def _tokens(value: str) -> list[str]:
    # Citadel's own confirmation mail says Citadel and never HF, so the
    # scraper's parenthetical source label has to come off before matching.
    bare = plain_company(value)
    return [w for w in _WORD_RE.findall(bare.lower()) if w not in _STOPWORDS]


def _has_word(haystack: str, token: str) -> bool:
    """Whole-word test. "ADM" is a real company on this board, and a substring
    test paired it with a Barcelona booking mail and a the school payment receipt."""
    return re.search(rf"\b{re.escape(token)}\b", haystack) is not None


_DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
                         "\u2014": "-", "\u2212": "-"})


def _norm(value: str) -> str:
    # Dashes fold to a hyphen: Citadel's board title separates "Quantitative
    # Trader" from "University Graduate (Europe)" with an en dash and its
    # confirmation uses a hyphen, so the title tie-break between his two Citadel
    # roles never fired and the mail went unmatched (2026-09-13).
    return re.sub(r"\s+", " ", (value or "").replace("\u00a0", " ").translate(_DASHES)).strip()


# A waiting period before he may apply again. Found by pattern as well as by
# the model because missing one is expensive and quiet: Flow Traders' rejection
# of 2026-09-17 ends "we have a cool-down period of 12-months before we can
# accept a new application for this position", and nothing in Jobfeed would
# have stopped a second application inside that year. The cue and the period
# must share one sentence, so "12 months of experience" never counts.
_COOLDOWN_CUE_RE = re.compile(
    r"cool(?:ing)?[\s-]*(?:down|off)|re-?apply|re-?application|apply again"
    r"|new application|further application|another application|wait(?:ing)? period",
    re.IGNORECASE,
)
_PERIOD_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                 "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
                 "twelve": 12, "eighteen": 18, "twenty-four": 24}
_PERIOD_RE = re.compile(
    r"\b(\d{1,2}|" + "|".join(sorted(_PERIOD_WORDS, key=len, reverse=True)) +
    r")[\s-]*(month|year)s?\b", re.IGNORECASE)


def _add_months(start: date, months: int) -> date:
    year, month = divmod(start.month - 1 + months, 12)
    year, month = start.year + year, month + 1
    for day in (start.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, 28)


def find_cooldown(text: str, sent) -> tuple[str, str]:
    """(end date, sentence) for a stated waiting period, or ("", "")."""
    from applications import mail_llm as application_mail_llm
    start = application_mail_llm.mail_date(sent)
    if start is None:
        return "", ""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if not _COOLDOWN_CUE_RE.search(sentence):
            continue
        period = _PERIOD_RE.search(sentence)
        if not period:
            continue
        raw = period.group(1).lower()
        count = int(raw) if raw.isdigit() else _PERIOD_WORDS[raw]
        months = count * (12 if period.group(2).lower() == "year" else 1)
        if not 0 < months <= 60:
            continue
        return _add_months(start, months).isoformat(), " ".join(sentence.split())
    return "", ""


def message_key(message: dict) -> str:
    """Stable identity for a mail. The mailbox CLI exposes an IMAP uid, which is
    only unique within a folder and is reused if a folder is rebuilt, so the key
    also folds in the parts of the message that cannot change."""
    raw = "|".join(str(message.get(k, "")) for k in ("uid", "date", "from", "subject"))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


def fetch_messages(since: str, limit: int = 200, folder: str = "INBOX") -> list[dict]:
    """Read the mailbox through the existing read-only CLI. No IMAP code and no
    second credential path: `applications/account.py` already established this
    seam and the Keychain fallback it needs on the M1."""
    if not MAILBOX.is_file():
        raise RuntimeError(f"mailbox CLI not found at {MAILBOX}")
    proc = subprocess.run(
        [str(MAILBOX), "--since", since, "--limit", str(limit), "--body",
         "--folder", folder],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"mailbox failed: {proc.stderr.strip()[:200]}")
    payload = json.loads(proc.stdout or "[]")
    return payload if isinstance(payload, list) else payload.get("messages", [])


# A graduate programme is an application with no `seen_jobs` row: the sweep
# found it on a programme page, the scrape never saw it. Eight of the twelve
# confirmations the first live scan could not place were these.
CAMPUS_PREFIX = "campus:"


def candidate_campus(db: JobDB) -> list[dict]:
    """Programmes he has ticked as applied, shaped like a job row so one matcher
    serves both. `campus_state` itself is never written here: it holds his own
    marks and nothing else."""
    cur = db.conn.execute(
        "SELECT key, firm, programme FROM campus_state WHERE state='applied'"
    )
    return [
        {"id": f"{CAMPUS_PREFIX}{key}", "company": firm, "title": programme,
         "url": "", "status": "applied", "campus": True}
        for key, firm, programme in cur
    ]


def candidate_jobs(db: JobDB) -> list[dict]:
    placeholders = ",".join("?" * len(CANDIDATE_STATUSES))
    # Live rows first: a firm whose board churned ids leaves delisted copies of
    # the same posting, and attaching mail to one of those strands the status
    # where nothing displays it.
    cur = db.conn.execute(
        f"SELECT id, company, title, url, location, status FROM seen_jobs "
        f"WHERE status IN ({placeholders}) "
        f"ORDER BY CASE WHEN delisted_at IS NULL THEN 0 ELSE 1 END", CANDIDATE_STATUSES,
    )
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur]


# A requisition id with a letter inside it: SocGen's 26000I87 and 26000JW2.
# _REQ_RE cannot see these, and the job id's own "socgen_" prefix defeats its
# word boundary, so the id's last segment is taken as-is when it carries at
# least four digits. SocGen's confirmation of 2026-09-18 had "-26000I87" as its
# whole subject and still reached the queue as naming no role.
_ID_TAIL_RE = re.compile(r"^(?=(?:[A-Z]*\d){4})[A-Z0-9]{6,}$")


def _req_ids(job: dict) -> list[str]:
    found = _REQ_RE.findall(f"{job.get('id', '')} {job.get('url', '')}".upper())
    tail = str(job.get("id", "")).upper().rsplit("_", 1)[-1]
    if "_" in str(job.get("id", "")) and _ID_TAIL_RE.match(tail) and tail not in found:
        found.append(tail)
    return found


def _firm_key(company: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", company or "").strip().lower()


_ROLE_WORD_RE = re.compile(r"[a-z][a-z-]{3,}")
# Title words that appear in any recruiting mail, so they single out no role.
_GENERIC_ROLE_WORDS = frozenset({
    "analyst", "associate", "trader", "graduate", "programme", "program", "intern",
    "internship", "summer", "full", "time", "full-time", "role", "position", "team",
    "market", "markets", "hours", "business", "global", "junior", "senior", "apej",
    "emea", "apac", "americas", "campus", "career", "careers", "talent", "entry",
    "level", "start", "starts", "united", "states", "kingdom",
})


def names_one_role(job: dict, candidates: list[dict], message: dict) -> bool:
    """Whether the message singles `job` out from the other roles at its firm.

    The model is told to leave the role blank when two fit equally well, and on
    2026-09-17 it did not: Goldman's "Thank You for Applying" and DRW's "Thank
    you for applying to DRW" named no role, yet each landed on the older of two
    open applications at that firm. Checking only that the id was one of the
    candidates let a guess through. With one role at the firm the firm is
    enough; with several, the mail must carry this role's requisition id or a
    title or location word the others at that firm do not share — Citi's two
    confirmations that day said "Paris" and "London", UBS's said "Beijing"."""
    firm = _firm_key(job.get("company", ""))
    siblings = [c for c in candidates
                if c["id"] != job["id"] and not c.get("campus")
                and _firm_key(c.get("company", "")) == firm]
    if not siblings:
        return True
    haystack = " ".join(
        _norm(str(message.get(k, ""))) for k in ("subject", "body")).lower()
    if any(req.lower() in haystack for req in _req_ids(job)):
        return True

    def words(role: dict) -> set[str]:
        return set(_ROLE_WORD_RE.findall(
            f"{role.get('title', '')} {role.get('location', '')}".lower()))

    shared = set().union(*(words(s) for s in siblings)) | _GENERIC_ROLE_WORDS | _STOPWORDS
    if any(_has_word(haystack, w) for w in words(job) - shared):
        return True

    # A two-word phrase from the title that no sibling title has. Single generic
    # words are too common to count, but the phrase they form is not: Macquarie's
    # rejection of 2026-08-21 named the "2027 Commodities and Global Markets
    # Summer Internship Program", and "summer internship" is the only thing that
    # separates that role from the Graduate Programme with the same stem.
    def phrases(role: dict) -> set[str]:
        tokens = re.findall(r"[a-z0-9]+", role.get("title", "").lower())
        return {f"{a} {b}" for a, b in zip(tokens, tokens[1:])}

    flat = " " + " ".join(re.findall(r"[a-z0-9]+", haystack)) + " "
    own = phrases(job) - set().union(*(phrases(s) for s in siblings))
    return any(f" {p} " in flat for p in own)


def match_job(message: dict, candidates: list[dict]) -> tuple[dict | None, str]:
    """Exactly one candidate, or nothing. Returns (job, reason)."""
    haystack = " ".join(
        _norm(str(message.get(k, ""))).lower() for k in ("from", "subject", "body")
    )
    if not haystack.strip():
        return None, "empty message"
    hits: list[tuple[dict, list[str], bool]] = []
    for job in candidates:
        why: list[str] = []
        company_tokens = _tokens(job.get("company", ""))
        if company_tokens and all(_has_word(haystack, t) for t in company_tokens):
            why.append("company")
        req = next((r for r in _req_ids(job) if r.lower() in haystack), "")
        if req:
            why.append(f"req {req}")
        # The title never qualifies a candidate on its own. A campus programme
        # called "Graduate Programme" matched Maven's and Shell's mail that way
        # and made both ambiguous against Deutsche Bank.
        if why:
            title = _norm(job.get("title", "")).lower()
            if len(title) > 12 and title in haystack:
                why.append("title")
            hits.append((job, why, bool(req)))
    if not hits:
        return None, "no candidate matched"
    # Two roles at one firm match its name equally; only the requisition id
    # separates them, which is why bp's two rejections went unattached.
    # A firm he ticked on the campus page and also has a board row for matches
    # twice. The board row is the concrete posting carrying the CRM status; the
    # campus tick is evidence beside it, so it never wins a tie.
    board = [h for h in hits if not h[0].get("campus")]
    if board and len(board) < len(hits):
        hits = board
    by_req = [h for h in hits if h[2]]
    if len(by_req) == 1:
        job, why, _ = by_req[0]
        return job, "matched on " + " + ".join(why)
    if len(hits) > 1:
        # The title breaks a tie but never creates a match. bp's two rejections
        # name the same firm and differ only by "... - Singapore (Aug 2027)"
        # against "... - China (Aug 2027)", which is exactly the role title.
        by_title = [h for h in hits if "title" in h[1]]
        if len(by_title) == 1:
            job, why, _ = by_title[0]
            return job, "matched on " + " + ".join(why)
        return None, "ambiguous: " + ", ".join(j["id"] for j, _, _ in hits[:4])
    job, why, _ = hits[0]
    return job, "matched on " + " + ".join(why)


def is_forward_or_campus(job: dict, current: str, proposed: str) -> bool:
    return True if job.get("campus") else is_forward(current, proposed)


def suggest_company(db: JobDB, message: dict) -> str:
    """A confirmation whose role is not in the acted-on pool is still worth
    surfacing: the dry run found eight of them, all real applications sitting at
    `new` because the CRM was never updated. This only ever proposes, so a hit
    here is a one-click adoption in the UI and never an automatic write."""
    haystack = " ".join(
        _norm(str(message.get(k, ""))).lower() for k in ("from", "subject", "body")
    )
    hits = []
    for (company,) in db.conn.execute(
        "SELECT DISTINCT company FROM seen_jobs WHERE status <> 'ignored' "
        "AND company <> ''"
    ):
        tokens = _tokens(company)
        if tokens and all(_has_word(haystack, t) for t in tokens):
            hits.append(company)
    return hits[0] if len(hits) == 1 else ""


def action_required(message: dict) -> str:
    """The sentence asking him to do something, or "". Independent of status:
    an assessment invitation and a plain "action required" both need doing."""
    subject = _norm(str(message.get("subject", "")))
    body = _norm(str(message.get("body", "")))
    text = f"{subject}. {body}"
    if not _CONTEXT_RE.search(text):
        return ""
    found = _ACTION_RE.search(text)
    if not found:
        return ""
    start = max(0, found.start() - 90)
    return text[start:found.end() + 120].strip()


def classify(message: dict) -> tuple[str, str]:
    """Return (status, verbatim quote). The quote is cut from the message text so
    a wrong classification is inspectable rather than merely asserted."""
    subject = _norm(str(message.get("subject", "")))
    body = _norm(str(message.get("body", "")))
    text = f"{subject}. {body}"
    if not _CONTEXT_RE.search(text):
        return "", ""
    for status, pattern in CLASSIFIERS:
        found = re.search(pattern, text, re.IGNORECASE)
        if not found:
            continue
        start = max(0, found.start() - 90)
        quote = text[start:found.end() + 90].strip()
        return status, quote
    return "", ""


def is_forward(current: str, proposed: str) -> bool:
    """A status only ever moves forward. A later "thank you for applying" mail
    from a firm that has already invited him must not drag `interview` back to
    `applied`."""
    return mail_rank(proposed) > mail_rank(current)


def llm_reading(message: dict, candidates: list[dict]) -> dict | None:
    """A model reading of one message, or None to fall back to the regex pass.
    Every field it returns has already been checked against the message."""
    try:
        from applications import mail_llm as application_mail_llm
        if not application_mail_llm.available():
            return None
        return application_mail_llm.read_message(message, candidates)
    except Exception:
        return None


def scan(db: JobDB, since: str, *, apply: bool = False, limit: int = 200,
         use_llm: bool = True) -> list[dict]:
    candidates = candidate_jobs(db) + candidate_campus(db)
    # The model is handed one row per firm: a campus tick is evidence beside a
    # board row, not an alternative to it, and offering both let Jump's
    # confirmation land on the tick while the board row read as never
    # acknowledged. The regex path already preferred the board row in a tie;
    # the model returns a single id, so the choice has to be removed upstream.
    board_firms = {j["company"] for j in candidates if not j.get("campus")}
    model_candidates = [
        j for j in candidates
        if not (j.get("campus") and j["company"] in board_firms)
    ]
    results: list[dict] = []
    for message in fetch_messages(since, limit=limit):
        key = message_key(message)
        if db.application_mail_seen(key):
            continue
        # This system's own alert mail is never about an application. "[job-
        # scraper] 9 sources broke" names Shell among the broken sources, and on
        # 2026-08-30 it was attached to his Shell Singapore role as its mail.
        if str(message.get("subject", "")).startswith(SELF_ALERT_PREFIX):
            continue
        sender = str(message.get("from", "")).lower()
        if any(host in sender for host in AGGREGATOR_SENDERS):
            continue
        # Checked on the subject alone: a real stage mail can mention resuming
        # somewhere in its body, but only a draft reminder leads with it.
        if _DRAFT_RESUME_RE.search(str(message.get("subject", ""))):
            continue

        # The model reads the sentence; the regex pass only knows wordings it
        # has already been taught. Every field the model returns has been
        # checked against the message itself, and a failed check drops the
        # whole reading rather than half of it, so the fallback is yesterday's
        # behaviour rather than a confident invention.
        # Only recruiting mail is worth a model call. His inbox is mostly
        # flights, receipts and newsletters, and a reading of those costs
        # ~1,800 tokens to conclude "none". The same cheap gate the regex pass
        # uses decides, so nothing is skipped that the regex would have read.
        looks_relevant = bool(_CONTEXT_RE.search(
            _norm(str(message.get("subject", ""))) + " "
            + _norm(str(message.get("body", "")))[:4000]))
        reading = (llm_reading(message, model_candidates)
                   if use_llm and looks_relevant else None)
        rejection_reason, reason_kind = "", ""
        reapply_kind, reapply_after, reapply_quote = "", "", ""
        if reading is not None:
            status, quote = reading["status"], reading["evidence"]
            action, task_key = reading["action"], reading["task_key"]
            action_url, deadline = reading["action_url"], reading["deadline"]
            job = next((c for c in candidates if c["id"] == reading["job_id"]), None)
            reason = "model matched" if job else "model found no role"
            if job is not None and not job.get("campus") and not names_one_role(
                    job, model_candidates, message):
                count = 1 + sum(
                    1 for c in model_candidates
                    if c["id"] != job["id"] and not c.get("campus")
                    and _firm_key(c.get("company", "")) == _firm_key(job.get("company", "")))
                reason = (f"model picked one of {count} {job.get('company', '')} roles; "
                          "the mail names none")
                job = None
            rejection_reason = reading.get("reason", "")
            reason_kind = reading.get("reason_kind", "")
            reapply_kind = reading.get("reapply_kind", "")
            reapply_after = reading.get("reapply_after", "")
            reapply_quote = reading.get("reapply_quote", "")
        else:
            status, quote = classify(message)
            job, reason = match_job(message, candidates)
            action, task_key = action_required(message), ""
            action_url, deadline = "", ""
        # The pattern backstops the model on the one field whose absence is
        # silent: a missed cool-down looks exactly like no cool-down.
        if looks_relevant and not reapply_after:
            found_after, found_quote = find_cooldown(
                str(message.get("subject", "")) + "\n" + str(message.get("body", "")),
                message.get("date", ""))
            if found_after:
                reapply_kind, reapply_after, reapply_quote = (
                    "fixed_duration", found_after, found_quote)

        outcome = "queued"
        if not status and not action:
            outcome = "unclassified"
        elif job is None:
            outcome = "unmatched"
            suggestion = suggest_company(db, message)
            if suggestion:
                reason = f"{reason}; untracked role at {suggestion}"
        elif not status:
            outcome = "note"
        elif not is_forward_or_campus(job, job["status"], status):
            outcome = "not_forward"
        elif job.get("campus"):
            # Evidence only. His tick is the state of a programme, so the mail
            # is recorded against it and changes nothing.
            outcome = "campus_note"
        elif apply:
            db.set_status(job["id"], status)
            job["status"] = status
            outcome = "applied"
        # A confirmation mail is the employer saying it holds the application.
        # Nothing else in this system can establish that: the agent is
        # forbidden to submit and the review gate only proves the form was
        # filled. It is stamped whatever the status does, because Shell
        # confirmed receipt in a mail that arrived after the role had already
        # moved to `oa` — not_forward, so the stamp never ran and the row read
        # as never confirmed.
        if apply and job is not None and not job.get("campus") and status == "applied":
            db.confirm_application_submission(
                job["id"], str(message.get("date", "")), quote
            )
        event = {
            "message_key": key,
            "action_required": action[:600],
            "task_key": task_key,
            "action_url": action_url,
            "action_deadline": deadline,
            "received_at": str(message.get("date", "")),
            "sender": str(message.get("from", ""))[:200],
            "subject": _norm(str(message.get("subject", "")))[:300],
            # A company token alone is no link. Unread by the model and carrying
            # no status, a mail that merely names the firm stays off the role:
            # the backfill hung his BNP payslips on BNP Hong Kong and Lufthansa
            # flights on the Deutsche Bank programme.
            "job_id": job["id"] if job and (reading is not None or outcome != "unclassified") else "",
            "proposed_status": status,
            "outcome": outcome,
            "evidence": quote[:600],
            "match_reason": reason[:200],
            "rejection_reason": rejection_reason[:600],
            "reason_kind": reason_kind,
            "reapply_kind": reapply_kind,
            "reapply_after": reapply_after,
            "reapply_quote": reapply_quote[:600],
        }
        # A dry run must not consume the mail. Recording it would mark every
        # message processed and the next --apply would skip the lot.
        if apply:
            db.record_application_mail(event)
        results.append(event)
    if apply:
        db.backfill_submission_confirmations()
    return results


def audit(db: JobDB, stale_days: int = 21) -> list[str]:
    """Read-only reconciliation for the vault's weekly refresh.

    The scan is deterministic, so it misses wordings it has never seen: bp's
    "we regret to advise" read as a confirmation until 2026-09-12. This says
    what the board claims against what the inbox recorded, so a miss surfaces
    once a week instead of never. It writes nothing.
    """
    lines: list[str] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()

    todo = db.open_application_actions()
    if todo:
        lines.append(f"{len(todo)} open action(s) from mail:")
        for row in todo[:10]:
            who = row["company"] or "unmatched"
            lines.append(f"  - {who}: {row['subject'][:66]}")

    pending = db.pending_application_mail()
    if pending:
        lines.append(f"{len(pending)} message(s) awaiting a decision on /applications:")
        for row in pending[:10]:
            lines.append(f"  - [{row['proposed_status']}] {row['subject'][:70]}")

    for job in candidate_jobs(db):
        events = db.application_mail_for_job(job["id"])
        latest = next((e for e in events if e["proposed_status"]), None)
        if latest is None:
            continue
        if mail_rank(latest["proposed_status"]) > mail_rank(job["status"]):
            lines.append(
                f"  - {job['company']}: board says {job['status']}, "
                f"inbox says {latest['proposed_status']} "
                f"({latest['received_at'][:10]}, {latest['subject'][:50]})"
            )

    unconfirmed = db.conn.execute(
        "SELECT j.company FROM application_workflows w JOIN seen_jobs j ON j.id=w.job_id "
        "WHERE w.submitted_confirmed_at IS NULL AND EXISTS (SELECT 1 FROM "
        "application_mail_events e WHERE e.job_id=w.job_id AND e.proposed_status='applied')"
    ).fetchall()
    if unconfirmed:
        lines.append(f"{len(unconfirmed)} workflow(s) with a confirmation mail but no stamp:")
        for (company,) in unconfirmed[:10]:
            lines.append(f"  - {company}")

    # Only while a confirmation could still be coming. Marex, Deutsche Bank,
    # Jump and Wintermute simply never write, and repeating that every Sunday
    # is how a report starts being skipped. Two days is long enough for an
    # autoresponder; past three weeks the silence is that firm's habit, not a
    # discrepancy.
    recent = db.conn.execute(
        "SELECT id, company, title FROM seen_jobs WHERE status='applied' "
        "AND applied_at IS NOT NULL AND applied_at < ? AND applied_at > ?",
        ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
         (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()),
    ).fetchall()
    silent = [r for r in recent if not db.application_mail_for_job(r[0])]
    if silent:
        lines.append(f"{len(silent)} recent application(s) the employer has not acknowledged:")
        for _, company, title in silent[:10]:
            lines.append(f"  - {company} — {str(title)[:52]}")

    cur = db.conn.execute(
        "SELECT j.company, w.status, w.updated_at FROM application_workflows w "
        "JOIN seen_jobs j ON j.id = w.job_id "
        "WHERE w.status NOT IN ('completed','failed') AND w.updated_at < ?", (cutoff,)
    )
    stuck = cur.fetchall()
    if stuck:
        lines.append(f"{len(stuck)} workflow(s) untouched for {stale_days}+ days:")
        for company, status, updated in stuck[:10]:
            lines.append(f"  - {company} — {status} since {updated[:10]}")

    return lines or ["board and inbox agree; nothing awaiting a decision"]


def audit_link_candidates(db: JobDB, since: str, limit: int = 40) -> dict:
    """Export only role links that merit an independent weekly body check.

    The weekly mail worker already reads the relevant inbox bodies.  This
    command therefore makes no mailbox or model call and emits no body text:
    only the stored header, current assignment and other acted-on roles at the
    same firm.  Single-role firms and subjects that already identify the
    assigned role are counted as covered and omitted to save review tokens.
    """
    from applications import mail_llm as application_mail_llm

    cutoff = date.fromisoformat(since)
    candidates = candidate_jobs(db) + candidate_campus(db)
    by_id = {str(candidate["id"]): candidate for candidate in candidates}
    rows = db.conn.execute(
        "SELECT message_key, received_at, sender, subject, job_id, "
        "proposed_status, outcome, action_required "
        "FROM application_mail_events WHERE job_id <> '' "
        "ORDER BY received_at DESC LIMIT 1000"
    ).fetchall()
    coverage = {
        "stored_links": 0,
        "single_role_firm": 0,
        "identified_in_subject": 0,
        "eligible": 0,
        "returned": 0,
        "capped": 0,
    }
    items: list[dict] = []
    firms: dict[str, dict] = {}

    def public_role(role: dict) -> dict:
        return {
            "id": str(role.get("id", "")),
            "title": str(role.get("title", "")),
            "location": str(role.get("location", "")),
        }

    for row in rows:
        (key, received_at, sender, subject, job_id, proposed_status,
         outcome, action_required) = row
        received = application_mail_llm.mail_date(received_at)
        if received is None or received < cutoff:
            continue
        assigned = by_id.get(str(job_id))
        if assigned is None:
            continue
        coverage["stored_links"] += 1
        firm = _firm_key(str(assigned.get("company", "")))
        same_firm = [candidate for candidate in candidates
                     if _firm_key(str(candidate.get("company", ""))) == firm]
        if len(same_firm) < 2:
            coverage["single_role_firm"] += 1
            continue
        # For this audit, campus programmes at the same firm are real sibling
        # candidates too.  The nightly gate deliberately ignores them when it
        # resolves a scraped-role match, so clear that marker only for this
        # subject-strength check.
        audit_roles = [dict(candidate, campus=False) for candidate in same_firm]
        audit_assigned = next(candidate for candidate in audit_roles
                              if candidate["id"] == assigned["id"])
        if names_one_role(audit_assigned, audit_roles,
                          {"subject": subject, "body": ""}):
            coverage["identified_in_subject"] += 1
            continue
        coverage["eligible"] += 1
        if len(items) >= limit:
            coverage["capped"] += 1
            continue
        firms.setdefault(firm, {
            "key": firm,
            "company": str(assigned.get("company", "")),
            "candidates": [public_role(candidate) for candidate in same_firm],
        })
        items.append({
            "message_key": key,
            "received_at": received_at,
            "sender": sender,
            "subject": subject,
            "proposed_status": proposed_status,
            "outcome": outcome,
            "has_action": bool(action_required),
            "firm_key": firm,
            "assigned_id": str(assigned.get("id", "")),
        })
    coverage["returned"] = len(items)
    return {"since": since, "coverage": coverage,
            "firms": list(firms.values()), "items": items}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--apply", action="store_true",
                        help="write unambiguous forward transitions; otherwise dry run")
    parser.add_argument("--audit", action="store_true",
                        help="read-only: report where the board and the inbox disagree")
    parser.add_argument(
        "--audit-link-candidates", action="store_true",
        help="read-only JSON: ambiguous stored role links for weekly body review",
    )
    args = parser.parse_args()
    since = args.since or (date.today() - timedelta(days=args.days)).isoformat()
    db = JobDB(os.environ.get("JOBS_DB", str(ROOT / "jobs.db")))
    if args.audit:
        try:
            for line in audit(db):
                print(line)
        finally:
            db.conn.close()
        return 0
    if args.audit_link_candidates:
        try:
            print(json.dumps(audit_link_candidates(db, since, args.limit),
                             ensure_ascii=False, indent=2))
        finally:
            db.conn.close()
        return 0
    try:
        events = scan(db, since, apply=args.apply, limit=args.limit)
    finally:
        db.conn.close()
    for event in events:
        if not event["proposed_status"]:
            continue
        print(f"{event['outcome']:13} {event['proposed_status']:9} "
              f"{event['job_id'] or event['match_reason']:58.58} "
              f"{event['subject'][:52]}")
        if event.get("rejection_reason") or event.get("reason_kind") not in ("", "none"):
            print(f"{'':23}reason [{event.get('reason_kind') or '?'}]: "
                  f"{event.get('rejection_reason') or '(none stated)'}")
        if event.get("reapply_after"):
            print(f"{'':23}no reapplying until {event['reapply_after']}: "
                  f"{event['reapply_quote']}")
        elif event.get("reapply_kind") == "cycle_rule":
            print(f"{'':23}application-cycle rule: {event['reapply_quote']}")
    print(f"{len(events)} new message(s) since {since}"
          f"{'' if args.apply else ' (dry run)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
