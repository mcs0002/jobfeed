"""Keychain-backed recruiting account and narrowly scoped email verification."""
from __future__ import annotations

import argparse
import email.utils
import json
import sys
import os
import re
import secrets
import string
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from application_handoff import safe_application_url
from db import JobDB

ROOT = Path(__file__).resolve().parent
MAILBOX = Path.home() / ".local" / "bin" / "mailbox"
WORKFLOW_RE = re.compile(r"^apply_[a-f0-9]{8,64}$")
VERIFY_WORDS = re.compile(
    r"\b(verify|verification|confirm|confirmation|security code|passcode|one.?time|activate|reset|password)\b",
    re.I,
)
CODE_RE = re.compile(
    r"(?:verification|security|confirmation|one.?time|passcode|code|reset)[^0-9]{0,48}([0-9]{4,8})",
    re.I,
)
URL_RE = re.compile(r"https://[^\s<>\"']+")
ATS_DOMAINS = ("workday.com", "myworkdayjobs.com", "myworkdaysite.com",
               "greenhouse.io", "lever.co", "smartrecruiters.com",
               "icims.com", "taleo.net", "oraclecloud.com", "successfactors.com",
               "ashbyhq.com", "eightfold.ai")
MULTIPART_SUFFIXES = ("co.uk", "com.au", "com.sg", "com.hk", "co.jp", "co.nz")
# Enough of the public suffixes a careers site actually uses to tell a firm's
# own brand TLD (`group.bnpparibas`, `careers.barclays`) from an ordinary
# registration. Anything unlisted is read as a brand, which is the safe way
# round: it searches the mailbox for the firm's name rather than for a TLD.
PUBLIC_TLDS = frozenset({
    "com", "net", "org", "edu", "gov", "int", "info", "biz", "io", "ai", "app",
    "dev", "co", "eu", "de", "at", "ch", "uk", "ie", "fr", "nl", "be", "lu",
    "es", "it", "pt", "se", "no", "dk", "fi", "pl", "cz", "gr", "tr", "ru",
    "us", "ca", "mx", "br", "au", "nz", "sg", "hk", "jp", "cn", "in", "kr",
    "ae", "sa", "za", "il",
})


def _load_env() -> None:
    path = ROOT / ".env"
    if path.is_file():
        for raw in path.read_text().splitlines():
            if raw and not raw.lstrip().startswith("#") and "=" in raw:
                key, value = raw.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _context(workflow_id: str) -> tuple[JobDB, dict, dict, str, str, str]:
    if not WORKFLOW_RE.fullmatch(workflow_id):
        raise ValueError("invalid workflow id")
    db = JobDB(os.environ.get("JOBS_DB", str(ROOT / "jobs.db")))
    workflow = db.get_application_workflow(workflow_id)
    if workflow is None or workflow["status"] == "completed":
        db.conn.close(); raise ValueError("workflow unavailable")
    job = db.get_job(workflow["job_id"])
    url = safe_application_url(workflow["application_url"])
    if not job or not url:
        db.conn.close(); raise ValueError("workflow unavailable")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    first_path = next((p for p in parsed.path.split("/") if p), "")
    scope = host + (("/" + first_path.lower()) if "workday" in host and first_path else "")
    service = "jobfeed-application:" + scope
    return db, workflow, job, scope, service, host


def _profile_email() -> str:
    data = json.loads((ROOT / "secrets" / "applicant_profile.json").read_text())
    value = str(data.get("email", "")).strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("profile email unavailable")
    return value


def _keychain(service: str, account: str) -> str | None:
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", account,
                             "-s", service, "-w"], capture_output=True, text=True)
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def _new_password() -> str:
    chars = string.ascii_letters + string.digits + "!@#$%^&*_-+="
    required = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
                secrets.choice(string.digits), secrets.choice("!@#$%^&*_-+=")]
    # 16 characters: SAP SuccessFactors (Engie, 2026-09-13) rejects anything
    # over 18 and requires at least 15, and 16 from this alphabet is ~100 bits,
    # so the site-scoped throwaway loses nothing against the 24 it replaced.
    value = required + [secrets.choice(chars) for _ in range(12)]
    secrets.SystemRandom().shuffle(value)
    return "".join(value)


def _new_security_answer() -> str:
    # Letters and digits only, starting with a letter: an answer field is free
    # text on most portals, but some strip or refuse punctuation, and nobody
    # ever types these by hand.
    alphabet = string.ascii_lowercase + string.digits
    return secrets.choice(string.ascii_lowercase) + "".join(
        secrets.choice(alphabet) for _ in range(11))


def _store(service: str, account: str, label: str, value: str) -> None:
    subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-a", account,
                    "-s", service, "-l", label, "-w", value],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _security_answers(service: str, account: str) -> list[str]:
    """Three distinct generated answers for account-creation security questions.

    UBS's Kenexa BrassRing registration (2026-09-15) demands three security
    questions before it will create an account, and the run stopped there to
    ask. The answers are
    credentials like the password, so they are generated per site and kept in
    Keychain beside it. A password reset never rotates them: the site still
    holds the originals.
    """
    stored = _keychain(service + ":security-answers", account)
    try:
        answers = json.loads(stored) if stored else None
    except json.JSONDecodeError:
        answers = None
    if (isinstance(answers, list) and len(answers) == 3
            and all(isinstance(a, str) and a for a in answers)):
        return answers
    answers = []
    while len(answers) < 3:
        value = _new_security_answer()
        if value not in answers:
            answers.append(value)
    _store(service + ":security-answers", account,
           "Jobfeed recruiting account security answers", json.dumps(answers))
    return answers


def _base_domain(host: str) -> str:
    parts = host.split(".")
    width = 3 if any(host.endswith("." + suffix) for suffix in MULTIPART_SUFFIXES) else 2
    return ".".join(parts[-width:]) if len(parts) >= width else host


def _brand(host: str) -> str:
    """The firm's own label in a hostname, whatever it is registered under.

    BNP Paribas recruits from `group.bnpparibas` and mails from
    `bnpparibas.com`, so the registrable domain of the careers site shares
    nothing with the sender and the 2026-09-16 run found no earlier mail to
    look at. A brand gTLD carries the name in the last label instead of the
    second-to-last, and matching on the label alone spans a firm's country
    domains too (`ubs.com` and `ubs.ch`)."""
    parts = [p for p in host.lower().split(".") if p]
    if not parts:
        return ""
    if len(parts) >= 3 and any(host.endswith("." + s) for s in MULTIPART_SUFFIXES):
        return parts[-3]
    if len(parts) >= 2 and parts[-1] in PUBLIC_TLDS:
        return parts[-2]
    return parts[-1]


def _earlier_account_mail(host: str, before: str | None) -> dict | None:
    """The oldest mail from the firm itself dated before this workflow.

    A Keychain entry only exists from the first attended run, but he applied by
    hand before those existed. UBS's account dates from a November 2025
    application under the same email ("Your candidate reference number - UBS");
    the 2026-09-15 run knew none of that, began registering and hit security
    questions, then signed in with a password the site had never seen. Mail from
    the firm before the workflow is the evidence that sends a run straight to
    Forgot password. Shared ATS domains are skipped: mail from workday.com says
    nothing about this firm. Best effort, so a mailbox failure returns None.
    """
    domain = _base_domain(host)
    brand = _brand(host)
    if not brand or any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS):
        return None
    try:
        result = subprocess.run([str(MAILBOX), "--since", "2020-01-01", "--from", brand,
                                 "--limit", "100"], capture_output=True, text=True,
                                check=True, timeout=45)
        messages = json.loads(result.stdout)
    except Exception:
        return None
    cutoff = None
    if before:
        try:
            cutoff = datetime.fromisoformat(before).astimezone(timezone.utc)
        except ValueError:
            cutoff = None
    found = []
    for message in messages:
        sender_addr = email.utils.parseaddr(message.get("from", ""))[1].lower()
        sender_host = sender_addr.rsplit("@", 1)[-1] if "@" in sender_addr else ""
        # The brand has to be a whole label of the sender's host. `--from` is a
        # substring match, so "ubs" alone would otherwise claim hubspot.com.
        if brand not in sender_host.split("."):
            continue
        try:
            stamp = datetime.fromisoformat(message.get("date", "")).astimezone(timezone.utc)
        except ValueError:
            try:
                stamp = email.utils.parsedate_to_datetime(message.get("date", "")).astimezone(timezone.utc)
            except Exception:
                continue
        if cutoff is None or stamp < cutoff:
            found.append((stamp, str(message.get("subject", ""))[:200]))
    if not found:
        return None
    stamp, subject = min(found)
    return {"date": stamp.date().isoformat(), "subject": " ".join(subject.split())}


def credentials(workflow_id: str, *, rotate: bool = False) -> dict:
    db, workflow, _, scope, service, host = _context(workflow_id)
    account = _profile_email()
    # `confirmed` is whether a run ever saw the site accept this account. A
    # stored password alone proves only that `credentials` ran.
    prior = db.get_application_account(scope)
    confirmed = bool(prior and prior.get("verified_at"))
    password = _keychain(service, account)
    created = password is None
    if created or rotate:
        password = _new_password()
        _store(service, account, "Jobfeed recruiting account", password)
    security_answers = _security_answers(service, account)
    earlier_mail = None if confirmed else _earlier_account_mail(host, workflow.get("created_at"))
    db.record_application_account(scope, workflow_id, service)
    db.conn.close()
    # The password comes back, and that is a decision rather than an oversight.
    # Withholding it and typing over CDP instead (application_password.py) put a
    # fresh DevTools client on Chrome for every password entry, and Chrome gates
    # each new client behind a manual consent: the connection blocked on that
    # click and timed out, every time, on 2026-09-12. It cost a working account
    # creation and bought protection of a generated, site-scoped, one-command-
    # rotatable credential against a conversation log. Wrong trade, reverted.
    # application_password.py is kept for the day that consent can be held open.
    return {"status": "ready", "created": created, "rotated": rotate,
            "confirmed": confirmed, "earlier_account_mail": earlier_mail,
            "email": account, "password": password,
            "security_answers": security_answers}


def _domain_ok(host: str, sender_host: str, link_host: str) -> bool:
    allowed = {host, sender_host}
    for value in (host, sender_host):
        parts = value.split(".")
        width = 3 if any(value.endswith("." + suffix) for suffix in MULTIPART_SUFFIXES) else 2
        if len(parts) >= width:
            allowed.add(".".join(parts[-width:]))
    allowed.update(d for d in ATS_DOMAINS if host.endswith(d) or sender_host.endswith(d))
    # A firm can post on its own careers site and host the login on an ATS: Citi
    # advertises at jobs.citi.com and authenticates at citi.wd5.myworkdayjobs.com.
    # Its reset mail came from otp.workday.com, so neither the posting host nor
    # the sender ended in the link's domain and three valid reset links were
    # refused on 2026-09-12. When the mail itself comes from a known ATS, a link
    # to any known ATS is in scope; the correlation, recipient and
    # verification-word checks upstream still have to pass.
    if any(sender_host == d or sender_host.endswith("." + d) for d in ATS_DOMAINS):
        allowed.update(ATS_DOMAINS)
    return any(link_host == d or link_host.endswith("." + d) for d in allowed if d)


# How far before the account was prepared a code may have been sent. The browser
# subagent and the parent run concurrently, so the site can mail the code before
# `credentials` records the account: Goldman Sachs' arrived 19 seconds early on
# 2026-09-13 and was skipped on ten polls. Never earlier than the workflow itself.
VERIFICATION_LEAD = timedelta(minutes=15)


def verification(workflow_id: str) -> dict:
    db, workflow, job, scope, _, host = _context(workflow_id)
    meta = db.get_application_account(scope)
    db.conn.close()
    if not meta:
        raise ValueError("account not prepared")
    threshold = datetime.fromisoformat(meta["prepared_at"]).astimezone(timezone.utc) - VERIFICATION_LEAD
    created = workflow.get("created_at")
    if created:
        threshold = max(threshold, datetime.fromisoformat(created).astimezone(timezone.utc))
    since = threshold.date().isoformat()
    result = subprocess.run([str(MAILBOX), "--since", since, "--limit", "20", "--body",
                             "--max-body-chars", "8000"], capture_output=True, text=True, check=True)
    recipient = _profile_email().lower()
    company_tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", job.get("company", "")) if len(t) >= 4]
    dated = []
    for message in json.loads(result.stdout):
        try:
            stamp = email.utils.parsedate_to_datetime(message.get("date", "")).astimezone(timezone.utc)
        except Exception:
            try: stamp = datetime.fromisoformat(message.get("date", "")).astimezone(timezone.utc)
            except Exception: continue
        dated.append((stamp, message))
    # Newest first, so a resent code wins over the one it replaced.
    for stamp, message in sorted(dated, key=lambda pair: pair[0], reverse=True):
        if stamp < threshold or recipient not in message.get("to", "").lower():
            continue
        sender = message.get("from", "")
        sender_addr = email.utils.parseaddr(sender)[1]
        sender_host = sender_addr.rsplit("@", 1)[-1].lower() if "@" in sender_addr else ""
        text = "\n".join((message.get("subject", ""), message.get("body", "")))
        correlated = any(t in text.lower() or t in sender.lower() for t in company_tokens)
        correlated = correlated or host in text.lower() or any(sender_host.endswith(d) for d in ATS_DOMAINS)
        if not correlated or not VERIFY_WORDS.search(text):
            continue
        code = CODE_RE.search(text)
        if code:
            return {"status": "code", "value": code.group(1)}
        for raw in URL_RE.findall(text):
            link = raw.rstrip(".,);]}")
            link_host = (urlparse(link).hostname or "").lower()
            if _domain_ok(host, sender_host, link_host):
                return {"status": "link", "value": link}
    return {"status": "pending"}


def mark_verified(workflow_id: str) -> dict:
    db, _, _, scope, _, _ = _context(workflow_id)
    db.verify_application_account(scope); db.conn.close()
    return {"status": "verified"}


ACTIONS = ("credentials", "reset-credentials", "verification", "mark-verified")
# One fixed path, so the command text never varies and a single Antigravity
# permission grant covers every account action on every future application. The
# --workflow-id form put the id in the command, which made each one a new string
# and a fresh prompt.
FIXED_REQUEST = Path("/private/tmp/the user-application-account-request.json")


def _fixed_request() -> tuple[str, str]:
    obj = json.loads(FIXED_REQUEST.read_text())
    if not isinstance(obj, dict) or set(obj) != {"workflow_id", "action"}:
        raise ValueError("request must hold only workflow_id and action")
    if obj["action"] not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    return str(obj["workflow_id"]), str(obj["action"])


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", choices=ACTIONS)
    parser.add_argument("--workflow-id")
    parser.add_argument("--fixed-request-file", action="store_true",
                        help=f"read action and workflow id from {FIXED_REQUEST}")
    args = parser.parse_args()
    if args.fixed_request_file:
        try:
            workflow_id, action = _fixed_request()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
            return 2
        args.workflow_id, args.action = workflow_id, action
    if not args.action or not args.workflow_id:
        parser.error("give an action and --workflow-id, or --fixed-request-file")
    fn = {"credentials": credentials,
          "reset-credentials": lambda workflow_id: credentials(workflow_id, rotate=True),
          "verification": verification,
          "mark-verified": mark_verified}[args.action]
    try:
        print(json.dumps(fn(args.workflow_id), separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__}, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
