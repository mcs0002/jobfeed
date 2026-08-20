#!/usr/bin/env python3
"""Generate a tailored cover-letter PDF for a single role.

Writes a one-page letter using **Sonnet 4.6** via the `claude` CLI (same
subprocess / auth pattern as tag.py — no ANTHROPIC_API_KEY) and renders it to
PDF with reportlab. The job description is fetched on demand via
enrich_descriptions.enrich_one (+ Workday routing) when the stored row has
none, because a letter written without the JD is generic and weak.

Personal facts (career narrative, current role, availability) come from
secrets/applicant_profile.json. The script fails loud if required fields are
missing — a silent generic letter is worse than a clear error.

This tool never sends anything anywhere. It writes a local PDF under
secrets/applications/<company>_<jobid>/cover_letter.pdf to review, edit, and
submit manually.

Usage:
  ./cover_letter.py --job-id <id>                          # read role from jobs.db
  ./cover_letter.py --url URL --title T --company C        # ad-hoc role
  ./cover_letter.py --job-id <id> --no-pdf                 # print text only
  ./cover_letter.py --job-id <id> --open                   # open the PDF after

The DB read uses the local jobs.db by default (JOBS_DB env to override). On a
dev machine with a stale DB, use --from-m1 to pull the live role over ssh.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import requests  # noqa: E402

from claude_cli import _claude_bin, NO_TOOLS_ARGS  # reuse CLI discovery  # noqa: E402
from scrapers.enrich.descriptions import enrich_one  # noqa: E402

SECRETS = os.path.join(ROOT, "secrets")
PROFILE_JSON = os.path.join(SECRETS, "applicant_profile.json")
CV_TXT = os.path.join(SECRETS, "profile_cv.txt")
SAMPLES_TXT = os.path.join(SECRETS, "cover_letter_samples.txt")
APPLICATIONS_DIR = os.path.join(SECRETS, "applications")

MODEL = "claude-sonnet-4-6"
TIMEOUT_SECONDS = 120
# Use the FULL stored description (enrich caps page text at 16k chars). Unlike
# the tagger (800-char excerpt), the letter wants every detail
# of the JD so it can reference the specific desk / products / programme — Sonnet
# has ample context for it.
MAX_DESCRIPTION_CHARS = 16000

_SYSTEM_TEMPLATE = """You are writing a cover letter on behalf of {applicant_name}, a finance graduate applying to a specific role. Produce a complete, ready-to-send letter in their own voice.

You are given: (1) their CV in plain text, (2) two of their own past cover letters as STYLE ANCHORS, (3) key profile facts, and (4) the target role (company, title, location, description). Tailor the letter to the role; never invent experience that is not in the CV.

VOICE & STYLE (match the anchors):
- Confident, precise, professional British/European English. Earnest, not boastful; specific, not generic.
- Four body paragraphs: (1) the role + who they are + why they are interested; (2) most relevant experience — {career_narrative}; (3) academic and quantitative foundation; (4) why this firm / desk specifically, plus availability.
- Reference concrete things from the job description (the desk, products, asset class, programme name) so it is clearly bespoke.
- ~350-420 words of body. One page.

AVAILABILITY: {availability_narrative}

OUTPUT FORMAT — output ONLY the letter, no preamble, no commentary, no code fences. Structure exactly:
- A recipient block: a line like "Hiring Team" (or the named team/division if the role implies one), then the company name, then the city/location. 2-3 short lines.
- A blank line, then a subject line beginning "Application for " naming the role and company.
- A blank line, then the greeting "Dear Hiring Team," (or a more specific addressee only if the description names one).
- A blank line, then the four body paragraphs, each separated by a blank line.
- A blank line, then "Yours sincerely," on its own line and "{applicant_name}" on the next line.
Do NOT include their name/address letterhead or a date line — those are rendered separately."""


_REQUIRED_PROFILE_FIELDS = ("career_narrative", "current_role", "availability_narrative")


def _load_inputs() -> tuple[dict, str, str]:
    """Load the applicant profile, CV text, and style samples from secrets/.
    Fails loudly if the profile is missing or lacks required fields — it's the
    whole point. A silent generic letter (missing career facts) is worse than
    a clear error."""
    if not os.path.exists(PROFILE_JSON):
        sys.exit(
            f"missing {PROFILE_JSON} — copy applicant_profile.example.json and fill it"
        )
    with open(PROFILE_JSON) as fp:
        profile = json.load(fp)
    missing = [f for f in _REQUIRED_PROFILE_FIELDS if not profile.get(f)]
    if missing:
        sys.exit(
            f"applicant_profile.json is missing required fields for cover letter "
            f"generation: {', '.join(missing)}. "
            f"See applicant_profile.example.json for the expected structure."
        )
    cv_text = ""
    if os.path.exists(CV_TXT):
        with open(CV_TXT) as fp:
            cv_text = fp.read()
    samples = ""
    if os.path.exists(SAMPLES_TXT):
        with open(SAMPLES_TXT) as fp:
            samples = fp.read()
    return profile, cv_text, samples


def ensure_description(job: dict) -> str:
    """Return the job description, fetching it on demand if the row has none.
    Workday rows are routed through WorkdayEnricher like the backfill does."""
    desc = (job.get("description") or "").strip()
    if desc:
        return desc
    url = job.get("url") or ""
    if not url:
        return ""
    try:
        from scrapers.enrich.workday_enrich import WorkdayEnricher, is_workday
        if is_workday(url):
            # Without the tenant/board config we can't hit the cxs API; fall back
            # to a plain GET, which yields the JS shell — better than nothing, and
            # most queued roles won't be Workday.
            from scrapers.enrich.descriptions import _load_workday_cfgs
            cfg = _load_workday_cfgs().get(job.get("company"))
            if cfg:
                text = WorkdayEnricher().description(
                    url, cfg["tenant"], cfg["board"], cfg.get("applied_facets")
                )
                if text:
                    return text
    except Exception:
        pass
    return enrich_one(url, requests.Session())


def build_system_prompt(profile: dict) -> str:
    """Build the system prompt from the template, injecting personal narrative
    from the profile. This keeps generic instructions in code and personal facts
    in the profile JSON."""
    return _SYSTEM_TEMPLATE.format(
        applicant_name=profile.get("full_name", "the applicant"),
        career_narrative=profile["career_narrative"],
        availability_narrative=profile["availability_narrative"],
    )


def build_payload(job: dict, cv_text: str, profile: dict, samples: str) -> str:
    """Assemble the stdin payload for the CLI: role + CV + profile + anchors."""
    desc = ensure_description(job)[:MAX_DESCRIPTION_CHARS] or "(no description available)"
    location = job.get("location") or " ".join(
        filter(None, [job.get("loc_city"), job.get("loc_country")])
    ) or "(unspecified)"
    facts = {
        "name": profile.get("full_name"),
        "current_role": profile["current_role"],
        "education": profile.get("education"),
        "languages": profile.get("languages"),
        "availability": profile.get("availability"),
    }
    return (
        "=== TARGET ROLE ===\n"
        f"Company: {job.get('company', '')}\n"
        f"Title: {job.get('title', '')}\n"
        f"Location: {location}\n"
        f"Description:\n{desc}\n\n"
        "=== KEY PROFILE FACTS ===\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=2)}\n\n"
        "=== CV (plain text) ===\n"
        f"{cv_text}\n\n"
        "=== STYLE ANCHORS (their own past letters — match voice, do not copy) ===\n"
        f"{samples}\n"
    )


def generate_cover_letter(job: dict, cv_text: str, profile: dict, samples: str,
                          timeout: int = TIMEOUT_SECONDS) -> str:
    """Subprocess the claude CLI (Sonnet 4.6) and return the letter text.
    Returns '' on failure (defensive — a letter step must never crash)."""
    bin_path = _claude_bin()
    if not bin_path:
        sys.exit("claude CLI not found — is it installed and authenticated?")
    system_prompt = build_system_prompt(profile)
    payload = build_payload(job, cv_text, profile, samples)
    try:
        # NO_TOOLS_ARGS + neutral cwd: the payload embeds the scraped job
        # description (hostile input); letter generation needs no tools and
        # no project-root cwd (.env / secrets/ live there).
        result = subprocess.run(
            [bin_path, "-p", *NO_TOOLS_ARGS, "--model", MODEL, system_prompt],
            input=payload, capture_output=True, text=True, timeout=timeout,
            cwd=tempfile.gettempdir(),
        )
    except subprocess.TimeoutExpired:
        print(f"claude CLI timed out after {timeout}s", file=sys.stderr)
        return ""
    if result.returncode != 0:
        err = (result.stderr or "").strip()[:300] or "unknown error"
        print(f"claude CLI exit {result.returncode}: {err}", file=sys.stderr)
        return ""
    text = (result.stdout or "").strip()
    # Strip an accidental ``` fence if the model added one despite instructions.
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text).strip()
    return text


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def validate_job_id(job_id: str) -> str:
    """Reject job_ids that aren't ATS-prefixed slugs (e.g. workday_12345).
    Legitimate IDs only contain [A-Za-z0-9_-]; anything else is bad input and a
    shell-injection vector once interpolated into a remote ssh command."""
    if not _JOB_ID_RE.match(job_id or ""):
        sys.exit(f"invalid --job-id {job_id!r}: expected an ATS slug "
                 f"(letters, digits, '_' and '-' only)")
    return job_id


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_")[:40] or "role"


def render_pdf(body: str, out_path: str, profile: dict, place_date: str) -> str:
    """Render the letter body to a one-page A4 PDF with a simple letterhead."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.enums import TA_JUSTIFY, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
    )

    name = profile.get("full_name", "")
    addr = profile.get("address", {})
    contact = " · ".join(filter(None, [
        ", ".join(filter(None, [addr.get("street"),
                                " ".join(filter(None, [addr.get("postal_code"),
                                                       addr.get("city")]))])),
        profile.get("email"),
        profile.get("phone"),
    ]))

    head = ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=14,
                          spaceAfter=2, leading=16)
    sub = ParagraphStyle("sub", fontName="Helvetica", fontSize=8.5,
                         textColor="#333333", leading=11)
    datestyle = ParagraphStyle("date", fontName="Helvetica", fontSize=9.5,
                               alignment=TA_RIGHT, leading=12)
    para = ParagraphStyle("para", fontName="Helvetica", fontSize=10.5,
                          alignment=TA_JUSTIFY, leading=14.5, spaceAfter=9)
    tight = ParagraphStyle("tight", fontName="Helvetica", fontSize=10.5,
                           leading=14, spaceAfter=0)

    flow = [
        Paragraph(name, head),
        Paragraph(contact, sub),
        Spacer(1, 4),
        HRFlowable(width="100%", thickness=0.6, color="#888888"),
        Spacer(1, 14),
        Paragraph(place_date, datestyle),
        Spacer(1, 14),
    ]

    def esc(t: str) -> str:
        return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    # Blocks separated by blank lines. The recipient block (first block, before
    # the "Application for" subject) is rendered tight (single-spaced lines);
    # everything else is a justified paragraph.
    blocks = [b for b in re.split(r"\n\s*\n", body.strip()) if b.strip()]
    for i, block in enumerate(blocks):
        lines = [esc(ln.strip()) for ln in block.splitlines() if ln.strip()]
        if i == 0 and len(lines) > 1:
            for ln in lines:
                flow.append(Paragraph(ln, tight))
            flow.append(Spacer(1, 9))
        else:
            flow.append(Paragraph("<br/>".join(lines), para))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=2.4 * cm, rightMargin=2.4 * cm,
        topMargin=2.0 * cm, bottomMargin=2.0 * cm,
        title=f"Cover Letter — {name}",
    )
    doc.build(flow)
    return out_path


M1_HOST = os.environ.get("M1_SSH", "m1")


def _fetch_job_m1(job_id: str) -> dict:
    """Pull one role's full record from the live DB on the M1 over ssh.
    The M4's local jobs.db is a stale seed, so real roles must come from M1."""
    remote = (
        "cd ~/projects/job_scraper && .venv/bin/python -c "
        "'import db,json,sys; j=db.JobDB(\"jobs.db\").get_job(sys.argv[1]); "
        "print(json.dumps(j) if j else \"\")' " + shlex.quote(job_id)
    )
    try:
        out = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=15", M1_HOST, remote],
            capture_output=True, text=True, timeout=40,
        )
    except subprocess.TimeoutExpired:
        sys.exit(f"timed out reaching {M1_HOST} over ssh")
    if out.returncode != 0:
        sys.exit(f"ssh {M1_HOST} failed: {(out.stderr or '').strip()[:200]}")
    payload = (out.stdout or "").strip()
    if not payload:
        sys.exit(f"job {job_id} not found in the live DB on {M1_HOST}")
    return json.loads(payload)


def _load_job(args) -> dict:
    if args.url or args.title or args.company:
        return {
            "id": args.job_id or _slug(args.company) + "_adhoc",
            "url": args.url or "",
            "title": args.title or "",
            "company": args.company or "",
            "location": args.location or "",
            "description": args.description or "",
        }
    if not args.job_id:
        sys.exit("provide --job-id, or --url/--title/--company for an ad-hoc role")
    if args.from_m1:
        return _fetch_job_m1(args.job_id)
    from db import JobDB
    db_path = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))
    job = JobDB(db_path).get_job(args.job_id)
    if not job:
        sys.exit(f"job {args.job_id} not found in {db_path} "
                 f"(use --from-m1 to read the live DB)")
    return job


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a tailored cover-letter PDF.")
    ap.add_argument("--job-id", help="role id in jobs.db")
    ap.add_argument("--from-m1", action="store_true",
                    help="fetch the role from the live DB on the M1 over ssh")
    ap.add_argument("--url")
    ap.add_argument("--title")
    ap.add_argument("--company")
    ap.add_argument("--location")
    ap.add_argument("--description", help="paste a JD instead of fetching it")
    ap.add_argument("--no-pdf", action="store_true", help="print text only")
    ap.add_argument("--open", action="store_true", help="open the PDF when done")
    args = ap.parse_args()
    if args.job_id:
        validate_job_id(args.job_id)

    profile, cv_text, samples = _load_inputs()
    job = _load_job(args)
    print(f"Generating cover letter: {job.get('title')} @ {job.get('company')} ...",
          file=sys.stderr)

    letter = generate_cover_letter(job, cv_text, profile, samples)
    if not letter:
        return 1

    if args.no_pdf:
        print(letter)
        return 0

    today = date.today()
    place_date = f"{addr_city(profile)}, {today.day}. {today.strftime('%B %Y')}"
    folder = os.path.join(
        APPLICATIONS_DIR, f"{_slug(job.get('company'))}_{job.get('id')}"
    )
    out_path = os.path.join(folder, "cover_letter.pdf")
    render_pdf(letter, out_path, profile, place_date)
    with open(os.path.join(folder, "cover_letter.txt"), "w") as fp:
        fp.write(letter + "\n")
    print(f"wrote {out_path}")
    if args.open:
        subprocess.run(["open", out_path], check=False)
    return 0


def addr_city(profile: dict) -> str:
    return profile.get("address", {}).get("city", "")


if __name__ == "__main__":
    sys.exit(main())
