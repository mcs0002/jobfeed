"""Screening-answer bank: answers the user gave during attended runs, kept.

A run that meets a question neither the profile nor the references answer asks
him once, and until 2026-10-10 the answer lived only in that Antigravity
conversation, so the next firm asked again. The bank keeps those answers in
`screening_answers` (jobs.db), lets him review them on `/answers`, and writes
the approved ones to `APPROVED_FILE`, which the browser agent reads with
`view_file` at the start of a run. No new command, no new grant.

The contract, enforced here and repeated in skills/assisted-apply:

- **Captured answers are proposals.** The status endpoint stores them as
  `proposed`; only the owner's `/answers` approval makes one reusable.
- **The profile is authoritative for personal facts.** Questions about
  education, employment, availability, work authorisation or contact details
  are classified `profile`: kept for review (so he can fold them into
  applicant_profile.json) but never approvable and never exported.
- **Demographic and sensitive questions are never stored.** The capture path
  drops them, and the export re-checks every row, so a classifier tightened
  later also withholds rows stored before.
- **Narrative free text belongs to applications/writer.py.** "Why this firm"
  answers and long prose are dropped at capture.
- **Eligibility answers defer to standard_questions.md.** They are exported
  marked `defer_to_rules`, and the skill derives the answer from the rules and
  profile dates; a stored answer is only a cross-check.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from jobfeed.db import plain_company

ROOT = Path(__file__).resolve().parent.parent
# Under secrets/: gitignored, and scripts/export_public.sh drops secrets/
# wholesale, so the file never reaches the public mirror or the R2 export.
APPROVED_FILE = ROOT / "secrets" / "screening_answers.json"

STATES = ("proposed", "approved", "retired")
# Categories a reviewed answer may carry and be reused under.
REUSABLE_CATEGORIES = ("eligibility", "declaration", "consent", "preference",
                       "skills", "logistics", "other")
# Kept for review, never approvable or exported.
PROFILE = "profile"
# Never stored at all.
SENSITIVE = "sensitive"
NARRATIVE = "narrative"
CATEGORIES = REUSABLE_CATEGORIES + (PROFILE,)

MAX_ANSWERS_PER_REPORT = 40
MAX_QUESTION = 500
MAX_ANSWER = 1000
# A longer answer is prose, which the writer owns.
NARRATIVE_ANSWER_CHARS = 400


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.IGNORECASE)


# Demographic and special-category data (GDPR art. 9/10 and the EEO set),
# plus the UK social-mobility block. Matched on the question, never stored.
_SENSITIVE_RE = _rx(
    r"\bgender\b", r"\bsex\b", r"\bsexual", r"\bpronoun", r"\btrans(gender)?\b",
    r"\blgbt", r"\bethnic", r"\brace\b", r"\bracial", r"\bdisab", r"\bveteran",
    r"\bmilitary (status|service)", r"\breligio", r"\bbelief", r"\bcaste\b",
    r"\bdate of birth\b", r"\bage\b", r"\bhow old\b", r"\bmarital", r"\bpregnan",
    r"\bhealth\b", r"\bmedical", r"\bneurodiver", r"\bpolitical", r"\btrade union",
    r"\bcriminal", r"\bconvict", r"\boffen[cs]e", r"\bfree school meals",
    r"\bsocio[- ]?economic", r"\bparents?'? (occupation|education|job)",
    r"\bfirst (generation|in (your|the) family)", r"\bcare leaver",
    r"\bcaring responsibilit", r"\bdiversity", r"\bequal opportunit",
    r"\bself[- ]identif", r"\bheritage\b", r"\bminority\b",
)
# Facts applicant_profile.json owns. Order: checked after eligibility, so
# "graduating in the next academic year" is an eligibility question.
_PROFILE_RE = _rx(
    r"\bauthori[sz]", r"\bright to work", r"\bwork permit", r"\bsponsor",
    r"\bvisa\b", r"\bcitizen", r"\bnationalit", r"\bstart date", r"\bavailab",
    r"\bearliest (start|date)", r"\bnotice period", r"\bgraduation date",
    r"\bexpected (to )?graduat", r"\bdegree\b", r"\bgpa\b", r"\bgrade",
    r"\buniversity\b", r"\bschool\b", r"\binstitution", r"\bfield of study",
    r"\bmajor\b", r"\bemployer", r"\bjob title", r"\bcurrent(ly)? (employ|work|role|company)",
    r"\bmost recent (employ|role|company|title)", r"\byears of (relevant )?experience",
    r"\bsalary", r"\bcompensation", r"\bpay expectation", r"\baddress\b",
    r"\bphone\b", r"\bemail address", r"\blinkedin", r"\blanguage", r"\bfluen",
    r"\bnative speaker", r"\bfull name", r"\bfirst name", r"\blast name",
    r"\bsurname", r"\bpostcode", r"\bpostal code", r"\bcity of residence",
    r"\bwhere do you live", r"\brelocat",
)
_ELIGIBILITY_RE = _rx(
    r"\beligib", r"\bpenultimate", r"\bfinal[- ]year", r"\bnext academic year",
    r"\b(currently|still) enrolled", r"\benrolled (as|for|in|at)",
    r"\bactive student", r"\breturning to (university|stud)",
    r"\bfull[- ]time for the (contract|internship|placement)",
    r"\bmeet (the )?(minimum )?(requirement|criteria)", r"\bgraduat",
    r"\bconvention de stage", r"\bstudent status",
)
_NARRATIVE_RE = _rx(
    r"^\s*why\b", r"\bwhy (do|are|did|would|this|our|us|you)", r"\bdescribe\b",
    r"\btell us\b", r"\bexplain\b", r"\bmotivat", r"\bcover[\s-]*letter",
    r"\bwhat (interests|excites|attracts) you", r"\bgive (us )?an example",
    r"\bin (\d+|a few|no more than \d+) words", r"\bshare (an|a) (example|time)",
    r"\bwhat makes you", r"\bstatement\b", r"\bessay",
)
_DECLARATION_RE = _rx(
    r"\bconflict of interest", r"\brelative", r"\bfamily member", r"\bpreviously (worked|employed|applied)",
    r"\bnon[- ]?compete", r"\brestrictive covenant", r"\bsanction", r"\bregulat",
    r"\bpolitically exposed", r"\bpublic official", r"\bbankrupt", r"\blitigation",
    r"\bdismiss", r"\bdeclar", r"\bdisclos",
)
_CONSENT_RE = _rx(r"\bconsent", r"\bagree\b", r"\bprivacy", r"\backnowledg",
                  r"\bterms\b", r"\brecord(ed|ing)\b", r"\btalent (pool|community)")
_PREFERENCE_RE = _rx(r"\bprefer", r"\brank", r"\bwhich (office|location|team|desk|division|business)",
                     r"\binterested in\b", r"\bbusiness area", r"\bfirst choice",
                     r"\bsecond choice")
_SKILLS_RE = _rx(r"\bproficien", r"\bexperience (with|in|using)", r"\bpython\b",
                 r"\bexcel\b", r"\bprogramming", r"\bcoding", r"\bskill",
                 r"\bcertif", r"\bcfa\b", r"\bbloomberg")
_LOGISTICS_RE = _rx(r"\btravel", r"\bhybrid", r"\bon[- ]?site", r"\bin the office",
                    r"\bshift", r"\bovertime", r"\bweekend", r"\bdriv(ing|er'?s) licen",
                    r"\bhow did you hear", r"\bsource\b", r"\breferr", r"\bcommute")


def normalise_question(text: str) -> str:
    """The key a question is filed under: accents, case, punctuation and the
    required/optional markers stripped, whitespace collapsed. Two forms asking
    the same question with different asterisks land on one key."""
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = re.sub(r"\((required|optional)\)|\b(required|optional)\b\s*$", " ", t)
    t = re.sub(r"[^\w]+", " ", t)
    return " ".join(t.split())[:300]


def classify(question: str, answer: str = "") -> str:
    """The category a question falls in, most protective rule first."""
    q = " ".join(str(question or "").split())
    if _SENSITIVE_RE.search(q):
        return SENSITIVE
    if _NARRATIVE_RE.search(q) or len(str(answer or "").strip()) > NARRATIVE_ANSWER_CHARS:
        return NARRATIVE
    if _ELIGIBILITY_RE.search(q):
        return "eligibility"
    if _PROFILE_RE.search(q):
        return PROFILE
    for name, rx in (("declaration", _DECLARATION_RE), ("consent", _CONSENT_RE),
                     ("preference", _PREFERENCE_RE), ("skills", _SKILLS_RE),
                     ("logistics", _LOGISTICS_RE)):
        if rx.search(q):
            return name
    return "other"


def approval_blocker(question: str, answer: str) -> str:
    """Why this question can never be approved, or "" when it can. Decided by
    the classifier on the wording, never by the category a row carries, so an
    edit to the category cannot smuggle a profile fact into the export."""
    cls = classify(question, answer)
    if cls == SENSITIVE:
        return "demographic or sensitive questions are never stored or reused"
    if cls == NARRATIVE:
        return "narrative answers are written for each application, not taken from the bank"
    if cls == PROFILE:
        return "a personal fact: it belongs in applicant_profile.json, which stays authoritative"
    return ""


def read_observation(raw) -> list[dict]:
    """The `answers` observation from a status request, validated to its shape:
    a list of {"question", "answer", "kind"?, "field"?}. Shape only; what may be
    stored is decided server-side in `capture`."""
    if not isinstance(raw, list) or len(raw) > MAX_ANSWERS_PER_REPORT:
        raise ValueError(f"answers is a list of at most {MAX_ANSWERS_PER_REPORT} items")
    out = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"question", "answer", "kind", "field"}:
            raise ValueError("each answer holds only question, answer, kind and field")
        question = " ".join(str(item.get("question") or "").split())
        answer = str(item.get("answer") or "").strip()
        if not question or not answer:
            raise ValueError("each answer needs its question and its answer")
        kind = str(item.get("kind") or "standing")
        if kind not in ("standing", "per-application"):
            raise ValueError("answer kind is standing or per-application")
        out.append({"question": question[:MAX_QUESTION], "answer": answer[:MAX_ANSWER],
                    "kind": kind, "field": " ".join(str(item.get("field") or "").split())[:200]})
    return out


def capture(db, workflow: dict, status: str, items: list[dict]) -> str:
    """Store what a run reported as proposals and say what happened.

    Never fails the status transition it rides on. Sensitive and narrative
    questions are dropped unstored; everything else lands as `proposed`."""
    if status not in ("review_ready", "needs_user_action"):
        return "answers not recorded: they ride on review_ready or needs_user_action"
    job = db.get_job(workflow["job_id"]) or {}
    firm = plain_company(job.get("company", ""))
    # The ATS as the run met it: the host of the application URL the workflow
    # snapshotted (myworkdayjobs.com, tal.net, …); seen_jobs carries no ATS.
    ats = urllib.parse.urlsplit(str(workflow.get("application_url") or "")).hostname or ""
    stored = dropped = 0
    for item in items:
        category = classify(item["question"], item["answer"])
        if category in (SENSITIVE, NARRATIVE):
            dropped += 1
            continue
        db.propose_screening_answer(
            question=item["question"], question_key=normalise_question(item["question"]),
            answer=item["answer"], category=category,
            scope_firm=firm if item["kind"] == "per-application" else "",
            seen_company=job.get("company", ""), seen_ats=ats,
            seen_field=item.get("field", ""), workflow_id=workflow["workflow_id"],
            job_id=workflow["job_id"])
        stored += 1
    note = f"answers proposed: {stored}"
    if dropped:
        note += f", {dropped} not stored (sensitive or narrative)"
    return note


def approved_export(db) -> dict:
    """The approved, reusable rows as the browser agent reads them. Every row
    is re-classified here, so nothing the current rules forbid leaves the DB."""
    rows = []
    for r in db.screening_answers(state="approved"):
        if approval_blocker(r["question"], r["answer"]):
            continue
        category = r["category"] if r["category"] in REUSABLE_CATEGORIES else "other"
        rows.append({
            "question_key": r["question_key"],
            "question": r["question"],
            "answer": r["answer"],
            "category": category,
            "firm": r["scope_firm"],
            "defer_to_rules": category == "eligibility",
            "approved_at": r["approved_at"],
        })
    rows.sort(key=lambda r: (r["question_key"], r["firm"]))
    return {
        "_contract": (
            "the user's approved screening answers. applicant_profile.json and "
            "standard_questions.md outrank every row: on any conflict stop and ask. "
            "A row with a firm applies only to that firm and replaces the general "
            "row for it. defer_to_rules rows are cross-checks: derive the answer "
            "from the truthfulness rules and profile dates."),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "answers": rows,
    }


def write_approved(db, path: Path | None = None) -> Path:
    """Rewrite the approved-answers file atomically, owner-readable only."""
    path = Path(path or APPROVED_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(approved_export(db), indent=1, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".screening_answers.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ── Owner actions (the /answers page). Each rewrites the approved file, so
# the agent's copy never lags what the page shows as approved. ──

def approve(db, answer_id: int, path: Path | None = None) -> None:
    row = db.get_screening_answer(answer_id)
    if row is None:
        raise KeyError(answer_id)
    blocker = approval_blocker(row["question"], row["answer"])
    if blocker:
        raise PermissionError(blocker)
    db.set_screening_answer_state(answer_id, "approved")
    write_approved(db, path)


def retire(db, answer_id: int, path: Path | None = None) -> None:
    if not db.set_screening_answer_state(answer_id, "retired"):
        raise KeyError(answer_id)
    write_approved(db, path)


def edit(db, answer_id: int, *, answer: str, category: str, scope_firm: str,
         path: Path | None = None) -> None:
    row = db.get_screening_answer(answer_id)
    if row is None:
        raise KeyError(answer_id)
    answer = str(answer or "").strip()[:MAX_ANSWER]
    if not answer:
        raise ValueError("an answer cannot be empty")
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    if row["state"] == "approved":
        blocker = approval_blocker(row["question"], answer)
        if blocker:
            raise PermissionError(blocker)
    db.edit_screening_answer(answer_id, answer=answer, category=category,
                             scope_firm=plain_company(scope_firm)[:200])
    write_approved(db, path)
