"""Read one recruiting email with the same model the tagger uses.

The deterministic pass is honest but blind: it only knows wordings it has seen,
so bp's "regret to advise" read as a confirmation, "Deutsche Bank" matched a
Deutsche Bahn booking on the single token `deutsche`, and three Shell mails
about one assessment appeared as three tasks. A model reads the sentence.

What it is NOT allowed to do is invent. Everything it returns is checked before
it is used, in the shape `application_limits.py` established:

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

import tag

STATUSES = ("applied", "oa", "interview", "offer", "rejected", "none")
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

Copy quotes exactly. Do not paraphrase, translate or summarise them."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "job_id", "action", "evidence", "task_key",
                 "action_url", "deadline"],
    "properties": {
        "status": {"type": "string", "enum": list(STATUSES)},
        "job_id": {"type": "string"},
        "action": {"type": "string"},
        "evidence": {"type": "string"},
        "task_key": {"type": "string"},
        "action_url": {"type": "string"},
        "deadline": {"type": "string"},
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
    return {
        "status": "" if status == "none" else status,
        "job_id": job_id,
        "action": action[:600],
        "evidence": evidence[:600],
        "task_key": re.sub(r"[^a-z0-9-]", "", str(reading.get("task_key", "")).lower())[:40],
        "action_url": url[:500],
        "deadline": deadline,
    }
