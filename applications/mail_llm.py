"""Read one recruiting email with the same model the tagger uses.

The deterministic pass is honest but blind: it only knows wordings it has seen,
so bp's "regret to advise" read as a confirmation, "Deutsche Bank" matched a
Deutsche Bahn booking on the single token `deutsche`, and three Shell mails
about one assessment appeared as three tasks. A model reads the sentence.

What it is NOT allowed to do is invent. Everything it returns is checked before
it is used, in the shape `applications/limits.py` established:

- the role must be one of the candidates handed to it, by id;
- the status must be in the controlled vocabulary;
- every quote must appear verbatim in the message that was sent.

Anything that fails a gate drops the whole reading and the caller falls back to
the regex pass, so the worst case is what we had yesterday rather than a
confident invention.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from jobfeed import tag
# The waiting-period vocabulary and the date arithmetic are shared with the
# regex path. They were duplicated here, and the two copies deriving a
# reapplication date independently is exactly how they drift apart.
# application_mail imports this module lazily, so importing it back at
# module level is not a cycle.
from applications.mail import _PERIOD_RE, _PERIOD_WORDS, _add_months

STATUSES = ("applied", "oa", "interview", "offer", "rejected", "none")
# Why an application was declined. A flat `rejected` hid the one distinction
# that matters: Cargill's "one or more of your responses indicated that you do
# not meet the minimum requirements" is a screening answer he can learn from,
# Flow Traders' "your test scores ... did not meet our global standards" is an
# assessment result, and neither is a rejection after an interview.
REASON_KINDS = ("assessment", "screening", "comparative_fit",
                "application_limit", "work_authorization", "position_filled",
                "interview", "other", "none")
MAX_BODY = 3000
TIMEOUT = 60

SYSTEM = """You read one email from a job applicant's inbox and report what it says.

Return JSON only, matching the schema. Rules:

- status: what the email tells the applicant about their application.
  applied   = the employer confirms they received it
  oa        = an online assessment, test or video interview is required
  interview = a human interview is offered or scheduled
  offer     = an offer of employment
  rejected  = the application is declined, however politely
  none      = anything else, including newsletters, account notices, travel,
              invoices, and mail unrelated to a job application.
- job_id: the id of the role this email is about, copied EXACTLY from the
  candidate list. Use "" when the email names no role you were given, or when
  two candidates fit equally well. Never guess between two firms.
- action: the single sentence telling the applicant to DO something, copied
  word for word from the email. "" when the email asks for nothing. A
  confirmation that merely says they will be in touch asks for nothing.
- evidence: the sentence that justifies the status, copied word for word.
- task_key: a short lowercase slug for the task, so several emails about the
  same assessment collapse into one. Example: "online-assessment",
  "video-interview", "interview-booking". "" when there is no action.
- action_url: the link the applicant must open to do the task, copied EXACTLY
  from the email. "" if the email gives none. Never a tracking pixel, an
  unsubscribe link, or the employer's home page.
- deadline: when the task is due, as YYYY-MM-DD, if the email states one either
  as a date or as a period ("within 7 days", "by 16 September"). Resolve a
  period against the email's own date, which is given to you. "" if none.
- reason: only when status is rejected, the sentence that says WHY, copied word
  for word ("We have reviewed your test scores and unfortunately they did not
  meet our global standards."). A bare "we will not be moving forward" is the
  decision, not a reason: it belongs in evidence, and reason is then "".
- reason_kind: why it was declined, from the reason sentence alone.
  assessment         = a test, online assessment or recorded video interview result
  screening          = an answer on the application did not meet a requirement
  comparative_fit    = another candidate was described as a closer fit
  application_limit  = a cap on applications per year, cycle or programme
  work_authorization = visa, sponsorship or right to work
  position_filled    = the role was filled, closed, cancelled or put on hold
  interview          = after an interview with a person
  other              = a stated reason that fits none of these
  none               = no reason given, or the email is not a rejection
- reapply_after: when the email states a waiting period before the applicant
  may apply again ("a cool-down period of 12 months", "you may reapply after
  six months"), the date that period ends as YYYY-MM-DD, counted from the
  email's own date. "" if none.
- reapply_quote: the sentence stating that waiting period, word for word. ""
  if none.

Copy quotes exactly. Do not paraphrase, translate or summarise them."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "job_id", "action", "evidence", "task_key",
                 "action_url", "deadline", "reason", "reason_kind",
                 "reapply_after", "reapply_quote"],
    "properties": {
        "status": {"type": "string", "enum": list(STATUSES)},
        "job_id": {"type": "string"},
        "action": {"type": "string"},
        "evidence": {"type": "string"},
        "task_key": {"type": "string"},
        "action_url": {"type": "string"},
        "deadline": {"type": "string"},
        "reason": {"type": "string"},
        "reason_kind": {"type": "string", "enum": list(REASON_KINDS)},
        "reapply_after": {"type": "string"},
        "reapply_quote": {"type": "string"},
    },
}


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


def _verbatim(quote: str, haystack: str) -> bool:
    """The quote has to be in the message. Whitespace and case are forgiven;
    wording is not. This is what stops a plausible sentence being invented."""
    if not quote:
        return True
    return _norm(quote) in _norm(haystack)


def mail_date(raw) -> date | None:
    """The email's own date, from ISO or RFC 2822, or None."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError):
        return None


_CYCLE_RULE_RE = re.compile(
    r"\b(?:per|same|each|one)\s+(?:academic|recruitment|application|hiring)\s+"
    r"(?:year|cycle)\b|\b(?:academic|recruitment|application|hiring)\s+cycle\b",
    re.IGNORECASE)
_APPLICATION_LIMIT_RE = re.compile(
    r"\b(?:only|maximum|max(?:imum)?|limit(?:ed)?)\b.{0,45}\bapplications?\b"
    r"|\bapplications?\b.{0,45}\b(?:only|maximum|max(?:imum)?|limit(?:ed)?)\b",
    re.IGNORECASE)


def checked_reapply(after: str, quote: str, text: str, sent) -> tuple[str, str, str]:
    """Return (kind, end date, quote) only for a rule the quote supports.

    The model does not get to turn a recruitment cycle into a calendar date.
    A fixed date is computed here from an explicit duration in the verbatim
    sentence. Cycle rules are preserved as rules with no date."""
    if not quote or not _verbatim(quote, text):
        return "", "", ""
    if _CYCLE_RULE_RE.search(quote) and _APPLICATION_LIMIT_RE.search(quote):
        return "cycle_rule", "", quote
    start = mail_date(sent)
    period = _PERIOD_RE.search(quote)
    if start is None or not period:
        return "", "", ""
    raw = period.group(1).lower()
    count = int(raw) if raw.isdigit() else _PERIOD_WORDS[raw]
    months = count * (12 if period.group(2).lower() == "year" else 1)
    if not 0 < months <= 60:
        return "", "", ""
    end = _add_months(start, months)
    # The model-supplied date is accepted only when it equals the deterministic
    # calculation. It is otherwise discarded with the rule, not corrected
    # silently, so a prompt regression is visible in tests and dry runs.
    if after != end.isoformat():
        return "", "", ""
    return "fixed_duration", end.isoformat(), quote


def classify_reason_kind(reason: str) -> str:
    """Deterministically classify one already-verbatim rejection reason.

    The model chooses the sentence; code chooses the category. Therefore the
    same sentence cannot flip between `screening` and `other` across calls."""
    text = _norm(reason)
    if not text:
        return "none"
    rules = (
        ("application_limit", r"\b(?:only|maximum|max(?:imum)?|limit(?:ed)?)\b.{0,60}\bapplications?\b|\bapplications?\b.{0,60}\b(?:academic year|recruitment cycle|application cycle)\b"),
        ("work_authorization", r"\b(?:work authori[sz]ation|right to work|visa|sponsor(?:ship|ing)?|work permit)\b"),
        ("assessment", r"\b(?:assessment|test scores?|online test|video interview|hackerrank|codility|codesignal)\b"),
        ("position_filled", r"\b(?:position|role|vacancy)\b.{0,35}\b(?:filled|closed|cancelled|canceled|on hold)\b"),
        ("interview", r"\b(?:after|following|based on)\b.{0,25}\binterview\b|\binterview (?:performance|feedback)\b"),
        # Comparative fit must precede screening: "closer match to our
        # requirements" is not evidence that an application answer failed.
        ("comparative_fit", r"\b(?:other|another) candidates?\b.{0,90}\b(?:closer|more closely|better|stronger)\b|\b(?:closer|more closely|better|stronger)\b.{0,90}\b(?:fit|match|align)\b"),
        ("screening", r"\b(?:your (?:answer|response)|responses? indicated|minimum requirements?|essential requirements?|eligibility criteria|did not meet|do not meet)\b|\bdo not feel\b.{0,45}\b(?:experience|background|skills)\b.{0,45}\b(?:aligns?|matches?)\b.{0,25}\brequirements?\b"),
    )
    for kind, pattern in rules:
        if re.search(pattern, text, re.IGNORECASE):
            return kind
    return "other"


def available() -> bool:
    cfg = tag._openai_cfg()
    return bool(cfg["base_url"] and cfg["api_key"] and cfg["model"])


def read_message(message: dict, candidates: list[dict]) -> dict | None:
    """One reading, fully checked, or None to fall back to the regex pass."""
    import requests

    cfg = tag._openai_cfg()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        return None
    body = str(message.get("body", ""))[:MAX_BODY]
    text = "\n".join((str(message.get("subject", "")), body))
    roles = "\n".join(
        f"{job['id']} | {job.get('company','')} | {job.get('title','')}"
        for job in candidates
    )
    user = (
        f"CANDIDATE ROLES (id | firm | title):\n{roles or '(none)'}\n\n"
        f"EMAIL\nDate: {message.get('date','')}\n"
        f"From: {message.get('from','')}\n"
        f"Subject: {message.get('subject','')}\n\n{body}"
    )
    payload = {
        "model": cfg["model"],
        "max_tokens": cfg["max_tokens"],
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "mail_reading", "strict": True, "schema": SCHEMA}},
    }
    for key, val in (cfg.get("extra") or {}).items():
        payload[key] = val
    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}",
                     "content-type": "application/json"},
            json=payload, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        out = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        reading = json.loads(out)
    except Exception:
        return None
    if not isinstance(reading, dict):
        return None

    status = str(reading.get("status", "")).strip()
    if status not in STATUSES:
        return None
    job_id = str(reading.get("job_id", "")).strip()
    if job_id and job_id not in {c["id"] for c in candidates}:
        return None
    action = str(reading.get("action", "")).strip()
    evidence = str(reading.get("evidence", "")).strip()
    if not _verbatim(action, text) or not _verbatim(evidence, text):
        return None
    # The link has to be one the email actually contains, and has to be a link.
    url = str(reading.get("action_url", "")).strip()
    if url and (not url.lower().startswith("https://") or url not in text):
        url = ""
    deadline = str(reading.get("deadline", "")).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
        deadline = ""
    # The reason and the cool-down are blanked when they fail a check, rather
    # than dropping the whole reading as a bad status or evidence quote does:
    # losing a correct rejection to protect its explanation would be backwards.
    reason = str(reading.get("reason", "")).strip()
    if status != "rejected" or not _verbatim(reason, text):
        reason = ""
    reason_kind = classify_reason_kind(reason) if status == "rejected" else ""
    reapply_kind, reapply_after, reapply_quote = checked_reapply(
        str(reading.get("reapply_after", "")).strip(),
        str(reading.get("reapply_quote", "")).strip(),
        text, message.get("date", ""))
    return {
        "status": "" if status == "none" else status,
        "job_id": job_id,
        "action": action[:600],
        "evidence": evidence[:600],
        "task_key": re.sub(r"[^a-z0-9-]", "", str(reading.get("task_key", "")).lower())[:40],
        "action_url": url[:500],
        "deadline": deadline,
        "reason": reason[:600],
        "reason_kind": reason_kind,
        "reapply_kind": reapply_kind,
        "reapply_after": reapply_after,
        "reapply_quote": reapply_quote[:600],
    }
