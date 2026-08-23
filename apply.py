#!/usr/bin/env python3
"""Assisted apply: open a role's application form in a real, logged-in browser
with the standard fields pre-filled and the CV + cover letter ready to attach,
then PAUSE so the user reviews and submits himself.

  *** This tool NEVER submits an application. ***
There is no .click() on any submit/apply button and no form .submit() anywhere.
It fills inputs and attaches files only; the final submit is always the human's
click. See test_apply_no_submit.py for the invariant guard.

Runs on the M4 (where the logged-in Chrome profile and the iCloud CV live). It
reuses cover_letter.py for the letter and a persistent Playwright Chrome profile
so ATS logins survive between runs. Greenhouse and Lever get real field autofill;
every other ATS still opens with a printed copy-paste card so nothing is ever a
dead end.

Usage:
  ./apply.py --job-id <id>                  # use the local jobs.db
  ./apply.py --queue                        # process local status='queued' roles
  ./apply.py --from-m1 --job-id <id>        # optional remote M1 flow
  ./apply.py --url URL --company C --title T
  ./apply.py --from-m1 --job-id <id> --no-letter   # skip letter gen (faster test)
  ./apply.py --url URL --headless          # autofill check without a window (testing)
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import cover_letter as cl  # noqa: E402

SECRETS = cl.SECRETS
CHROME_PROFILE = os.path.join(SECRETS, "chrome_profile")
M1_HOST = os.environ.get("M1_SSH", "m1")

# --- ATS detection + field maps -------------------------------------------

def detect_ats(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "myworkdayjobs.com" in host or "workday" in host:
        return "workday"
    return "unknown"


def normalize_url(url: str, ats: str) -> str:
    """Map a stored URL to the human application page where they differ.
    SmartRecruiters rows store the API endpoint, not the careers page."""
    if ats == "smartrecruiters":
        m = re.search(r"/companies/([^/]+)/postings/([^/?]+)", url)
        if m:
            return f"https://jobs.smartrecruiters.com/{m.group(1)}/{m.group(2)}"
    if ats == "lever" and not url.rstrip("/").endswith("/apply"):
        return url.rstrip("/") + "/apply"
    return url


# Selector candidates per field, tried in order; first match wins. Plain inputs
# only — no buttons, by design.
ATS_FIELDS = {
    "greenhouse": {
        "first_name": ["#first_name", "input[name='first_name']",
                       "input[autocomplete='given-name']"],
        "last_name": ["#last_name", "input[name='last_name']",
                      "input[autocomplete='family-name']"],
        "email": ["#email", "input[name='email']", "input[type='email']"],
        "phone": ["#phone", "input[name='phone']", "input[type='tel']"],
    },
    "lever": {
        "full_name": ["input[name='name']"],
        "email": ["input[name='email']", "input[type='email']"],
        "phone": ["input[name='phone']", "input[type='tel']"],
        "org": ["input[name='org']"],
        "linkedin": ["input[name='urls[LinkedIn]']", "input[name='urls[Linkedin]']"],
    },
}

# File-input selectors for attaching the CV (resume) and cover letter.
FILE_FIELDS = {
    "resume": ["input[type='file'][id*='resume' i]", "input[name*='resume' i]",
               "input[type='file'][id*='cv' i]", "input[type='file']"],
    "cover": ["input[type='file'][id*='cover' i]", "input[name*='cover' i]"],
}


def _field_values(profile: dict) -> dict:
    addr = profile.get("address", {})
    return {
        "first_name": profile.get("first_name", ""),
        "last_name": profile.get("last_name", ""),
        "full_name": profile.get("full_name", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "org": profile.get("current_employer", ""),
        "linkedin": profile.get("linkedin", ""),
        "city": addr.get("city", ""),
        "country": addr.get("country", ""),
    }


# German citizenship gives a clear work-right answer only where freedom of
# movement applies.  Outside that area we deliberately do not invent an
# immigration status: a visa, residence title or settled status is personal
# information that cannot be inferred from nationality.
_EU_EEA_COUNTRIES = {
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia",
    "czech republic", "denmark", "estonia", "finland", "france", "germany",
    "greece", "hungary", "iceland", "ireland", "italy", "latvia",
    "liechtenstein", "lithuania", "luxembourg", "malta", "netherlands",
    "norway", "poland", "portugal", "romania", "slovakia", "slovenia",
    "spain", "sweden",
}
_EU_EEA_CODES = {
    "at", "be", "bg", "hr", "cy", "cz", "dk", "ee", "fi", "fr", "de",
    "gr", "hu", "is", "ie", "it", "lv", "li", "lt", "lu", "mt", "nl",
    "no", "pl", "pt", "ro", "sk", "si", "es", "se",
}
_SWISS_NAMES = {"switzerland", "schweiz", "suisse", "svizzera", "ch"}
_UK_NAMES = {
    "united kingdom", "uk", "gb", "england", "scotland", "wales",
    "northern ireland",
}
_US_NAMES = {"united states", "united states of america", "usa", "us"}


def _matches_job_country(job: dict, names: set[str], codes: set[str] | None = None) -> bool:
    """Match a normalised country field first, then safe location tokens.

    Two-letter country codes are compared as tokens so e.g. ``de`` never
    accidentally matches a word such as ``Denmark``.
    """
    country = str(job.get("loc_country") or "").strip().casefold()
    if country:
        return country in names or country in (codes or set())
    location = str(job.get("location") or "").casefold()
    if any(name in location for name in names if len(name) > 2):
        return True
    tokens = set(re.findall(r"[a-z]+", location))
    return bool(tokens & (codes or set()) or tokens & {n for n in names if len(n) <= 2})


def work_authorization_answers(profile: dict, job: dict) -> dict[str, str]:
    """Derive role-specific work-authorisation answers conservatively.

    The current rule set is intentionally limited to German nationality.  For
    every other nationality the explicitly stored profile answers remain the
    fallback; this prevents a future profile change from silently receiving
    German/EU assumptions.
    """
    if str(profile.get("nationality") or "").strip().casefold() != "german":
        common = profile.get("common_answers", {})
        return {
            "market": "profile",
            "authorized_to_work": common.get("authorized_to_work", ""),
            "require_visa_sponsorship": common.get("require_visa_sponsorship", ""),
        }
    if _matches_job_country(job, _EU_EEA_COUNTRIES, _EU_EEA_CODES):
        return {
            "market": "EU/EEA",
            "authorized_to_work": "Yes — German/EU citizen.",
            "require_visa_sponsorship": "No.",
        }
    if _matches_job_country(job, _SWISS_NAMES):
        return {
            "market": "Switzerland",
            "authorized_to_work": (
                "Eligible as a German/EU citizen under EU/EFTA free movement; "
                "Swiss notification/permit formalities apply."
            ),
            "require_visa_sponsorship": (
                "No employer visa sponsorship required; Swiss registration/permit "
                "formalities still apply."
            ),
        }
    if _matches_job_country(job, _UK_NAMES):
        return {
            "market": "UK",
            "authorized_to_work": (
                "No — not from German nationality alone (unless a separate UK "
                "immigration status applies)."
            ),
            "require_visa_sponsorship": (
                "Yes, unless separate UK work authorization already exists."
            ),
        }
    if _matches_job_country(job, _US_NAMES):
        return {
            "market": "US",
            "authorized_to_work": (
                "No — not from German nationality alone (unless separate US work "
                "authorization exists)."
            ),
            "require_visa_sponsorship": (
                "Yes, unless separate US work authorization already exists."
            ),
        }
    return {
        "market": "country-specific",
        "authorized_to_work": (
            "Not established from German nationality alone — check the destination "
            "country's rules."
        ),
        "require_visa_sponsorship": "Country-specific check required.",
    }


def autofill(page, ats: str, profile: dict) -> list[str]:
    """Fill recognised fields from the profile. Returns labels of filled fields.
    Best-effort: a missing field is skipped, never fatal. Inputs only."""
    filled = []
    values = _field_values(profile)
    for field, selectors in ATS_FIELDS.get(ats, {}).items():
        value = values.get(field, "")
        if not value:
            continue
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible(timeout=1500):
                    loc.fill(value, timeout=2000)
                    filled.append(f"{field} = {value}")
                    break
            except Exception:
                continue
    return filled


def attach_files(page, cv_path: str, cover_pdf: str) -> list[str]:
    """Attach CV + cover letter to recognised file inputs. Best-effort."""
    attached = []
    for label, path in (("resume/CV", cv_path), ("cover letter", cover_pdf)):
        if not path or not os.path.exists(path):
            continue
        key = "cover" if "cover" in label else "resume"
        for sel in FILE_FIELDS[key]:
            try:
                loc = page.locator(sel).first
                if loc.count():
                    loc.set_input_files(path, timeout=3000)
                    attached.append(f"{label} -> {os.path.basename(path)}")
                    break
            except Exception:
                continue
    return attached


def copy_paste_card(profile: dict, job: dict, cv_path: str, cover_pdf: str) -> str:
    """Everything needed to fill a form by hand, in one block — the fallback for
    ATSes we don't autofill (Workday, SmartRecruiters, unknown)."""
    v = _field_values(profile)
    ca = profile.get("common_answers", {})
    work_auth = work_authorization_answers(profile, job)
    lines = [
        "  ---- COPY-PASTE CARD --------------------------------------------",
        f"  Name:        {v['full_name']}",
        f"  Email:       {v['email']}",
        f"  Phone:       {v['phone']}",
        f"  Location:    {v['city']}, {v['country']}",
        f"  LinkedIn:    {v['linkedin'] or '(none on file)'}",
        f"  Nationality: {profile.get('nationality','')}",
        f"  Work auth:   {work_auth['authorized_to_work']}",
        f"  Sponsorship: {work_auth['require_visa_sponsorship']}",
        f"  Earliest start: {ca.get('earliest_start_date','')}",
        f"  CV:          {cv_path}",
        f"  Cover letter: {cover_pdf or '(not generated)'}",
        "  -----------------------------------------------------------------",
    ]
    return "\n".join(lines)


# --- the per-job flow ------------------------------------------------------

def _resolve_cv_path(profile: dict) -> str:
    docs = profile.get("documents", {})
    base = os.path.expanduser(docs.get("base_dir", ""))
    cv = docs.get("cv", "")
    return os.path.join(base, cv) if base and cv else ""


def _load_apply_inputs(make_letter: bool) -> tuple[dict, str, str]:
    """Load the full cover-letter inputs or a minimal autofill-only profile.

    cl._load_inputs() correctly requires narrative fields for generated letters,
    but those fields should not block the --no-letter form-filling workflow.
    """
    if make_letter:
        return cl._load_inputs()
    if not os.path.exists(cl.PROFILE_JSON):
        sys.exit(
            f"missing {cl.PROFILE_JSON} — copy applicant_profile.example.json "
            "and fill it"
        )
    with open(cl.PROFILE_JSON) as fp:
        profile = json.load(fp)
    required = ("full_name", "first_name", "last_name", "email", "phone")
    missing = [field for field in required if not profile.get(field)]
    if missing:
        sys.exit("applicant_profile.json is missing autofill fields: "
                 + ", ".join(missing))
    return profile, "", ""


def apply_one(job: dict, profile: dict, cv_text: str, samples: str,
              make_letter: bool = True, headless: bool = False,
              use_m1: bool = False, web_mode: bool = False) -> None:
    from playwright.sync_api import sync_playwright

    url = job.get("url") or ""
    if not url:
        print("  ! no URL for this role — nothing to open", file=sys.stderr)
        return
    ats = detect_ats(url)
    target = normalize_url(url, ats)
    cv_path = _resolve_cv_path(profile)

    cover_pdf = ""
    if make_letter:
        print("  generating cover letter (Sonnet 4.6)...", file=sys.stderr)
        letter = cl.generate_cover_letter(job, cv_text, profile, samples)
        if letter:
            from datetime import date
            today = date.today()
            place_date = f"{cl.addr_city(profile)}, {today.day}. {today.strftime('%B %Y')}"
            folder = os.path.join(
                cl.APPLICATIONS_DIR, f"{cl._slug(job.get('company'))}_{job.get('id')}"
            )
            cover_pdf = cl.render_pdf(letter, os.path.join(folder, "cover_letter.pdf"),
                                      profile, place_date)
            with open(os.path.join(folder, "cover_letter.txt"), "w") as fp:
                fp.write(letter + "\n")
            print(f"  cover letter: {cover_pdf}", file=sys.stderr)

    os.makedirs(CHROME_PROFILE, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print(f"  opening [{ats}] {target}", file=sys.stderr)
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"  ! navigation issue: {exc}", file=sys.stderr)

        filled = autofill(page, ats, profile) if ats in ATS_FIELDS else []
        attached = attach_files(page, cv_path, cover_pdf)

        print("\n  === REVIEW BEFORE YOU SUBMIT ===")
        print(f"  Role: {job.get('title')} @ {job.get('company')}  [{ats}]")
        if filled:
            print("  Auto-filled:")
            for f in filled:
                print(f"    + {f}")
        else:
            print("  No fields auto-filled (unsupported ATS or form not detected).")
        if attached:
            print("  Attached:")
            for a in attached:
                print(f"    + {a}")
        elif cv_path:
            print(f"  Attach manually — CV: {cv_path}")
            if cover_pdf:
                print(f"                 Letter: {cover_pdf}")
        print()
        print(copy_paste_card(profile, job, cv_path, cover_pdf))
        print("\n  Review every field, attach/fix anything missing, then SUBMIT yourself.")

        if headless:
            ctx.close()
            return
        if web_mode:
            print("  Started from Jobfeed. Close the application browser window "
                  "when you are finished; update the status on the website.")
            try:
                # The web server has no interactive stdin. Keep the dedicated
                # Playwright window alive until the user closes it instead of
                # asking them to return to a terminal prompt.
                while ctx.pages:
                    ctx.pages[0].wait_for_event("close", timeout=0)
            except Exception:
                pass
            ctx.close()
            return
        try:
            input("  Press Enter here when you are done (browser stays open until then)... ")
        except EOFError:
            pass

        if job.get("id") and not str(job.get("id")).endswith("_adhoc"):
            where = "the M1" if use_m1 else "the local database"
            ans = input(
                f"  Mark {job.get('id')} as 'applied' in {where}? [y/N] "
            ).strip().lower()
            if ans == "y":
                if use_m1:
                    mark_applied_m1(job["id"])
                else:
                    mark_applied_local(job["id"])
        ctx.close()


def seed_login(url: str) -> None:
    """Open `url` in the persistent profile with a real window and wait.

    Every bank ATS in the queue (Avature, Workday) gates its application form
    behind an account, so an unseeded profile stops on the login page no matter
    how the run is driven — headless changes nothing about that. the user signs
    in HERE, by hand, in his own password manager; the session cookie then lives
    in CHROME_PROFILE and every later run lands inside the wizard instead.

    This function never types into the page. It navigates and waits.
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(CHROME_PROFILE, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"  ! navigation issue: {exc}", file=sys.stderr)
        print(f"\n  Signed-out profile: {CHROME_PROFILE}")
        print("  Sign in (or create the account) in the window that just opened.")
        print("  Nothing is typed for you and nothing is submitted.")
        try:
            input("  Press Enter here once you are logged in... ")
        except EOFError:
            pass
        # Report what the session looks like now, so the seeding is verifiable
        # rather than assumed.
        try:
            n_pw = len(page.query_selector_all("input[type=password]"))
            print(f"  now at: {page.url[:100]}")
            print(f"  password fields on page: {n_pw}"
                  + ("  (still a login wall — not seeded)" if n_pw else
                     "  (looks past the login)"))
        except Exception:
            pass
        ctx.close()


def mark_applied_m1(job_id: str) -> None:
    remote = (f"cd ~/projects/job_scraper && .venv/bin/python query.py "
              f"--mark {shlex.quote(job_id)} applied")
    out = subprocess.run(["ssh", "-o", "ConnectTimeout=15", M1_HOST, remote],
                         capture_output=True, text=True)
    if out.returncode == 0:
        print(f"  marked {job_id} applied on {M1_HOST}")
    else:
        print(f"  ! could not mark applied: {(out.stderr or '').strip()[:160]}",
              file=sys.stderr)


def mark_applied_local(job_id: str) -> None:
    from db import JobDB
    db_path = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))
    if JobDB(db_path).set_status(job_id, "applied"):
        print(f"  marked {job_id} applied in {db_path}")
    else:
        print(f"  ! job {job_id} not found in {db_path}", file=sys.stderr)


def fetch_queue_m1() -> list[dict]:
    """Pull all status='queued' roles from the live DB on the M1."""
    remote = (
        "cd ~/projects/job_scraper && .venv/bin/python -c "
        "'import db,json; print(json.dumps(db.JobDB(\"jobs.db\").fetch_jobs(status=\"queued\")))'"
    )
    out = subprocess.run(["ssh", "-o", "ConnectTimeout=15", M1_HOST, remote],
                         capture_output=True, text=True, timeout=40)
    if out.returncode != 0:
        sys.exit(f"ssh {M1_HOST} failed: {(out.stderr or '').strip()[:200]}")
    return json.loads((out.stdout or "[]").strip() or "[]")


def fetch_queue_local() -> list[dict]:
    """Read queued roles from this installation's jobs.db."""
    from db import JobDB
    db_path = os.environ.get("JOBS_DB", os.path.join(ROOT, "jobs.db"))
    return JobDB(db_path).fetch_jobs(status="queued")


def main() -> int:
    ap = argparse.ArgumentParser(description="Assisted apply (never auto-submits).")
    ap.add_argument("--job-id")
    ap.add_argument("--from-m1", action="store_true")
    ap.add_argument("--queue", action="store_true",
                    help=("process every status='queued' role from local jobs.db "
                          "(or M1 with --from-m1)"))
    ap.add_argument("--url")
    ap.add_argument("--title")
    ap.add_argument("--company")
    ap.add_argument("--location")
    ap.add_argument("--description")
    ap.add_argument("--no-letter", action="store_true",
                    help="skip cover-letter generation")
    ap.add_argument("--headless", action="store_true",
                    help="run without a window (autofill check only; no pause)")
    ap.add_argument("--web", action="store_true",
                    help="website-launched mode: wait until the browser is closed")
    ap.add_argument("--login", metavar="URL",
                    help="open URL in the persistent profile and wait while you "
                         "sign in, seeding the session for later runs")
    args = ap.parse_args()
    if args.web and args.headless:
        ap.error("--web and --headless cannot be combined")

    if args.login:
        seed_login(args.login)
        return 0
    if args.job_id:
        cl.validate_job_id(args.job_id)

    make_letter = not args.no_letter
    profile, cv_text, samples = _load_apply_inputs(make_letter)

    if args.queue:
        jobs = fetch_queue_m1() if args.from_m1 else fetch_queue_local()
        if not jobs:
            where = "the M1" if args.from_m1 else "the local database"
            print(f"queue empty (no roles with status='queued' in {where})")
            return 0
        print(f"draining {len(jobs)} queued role(s)\n")
        for i, job in enumerate(jobs, 1):
            print(f"[{i}/{len(jobs)}] {job.get('company')} — {job.get('title')}")
            apply_one(job, profile, cv_text, samples,
                      make_letter=make_letter, headless=args.headless,
                      use_m1=args.from_m1, web_mode=args.web)
        return 0

    job = cl._load_job(args)
    apply_one(job, profile, cv_text, samples,
              make_letter=make_letter, headless=args.headless,
              use_m1=args.from_m1, web_mode=args.web)
    return 0


if __name__ == "__main__":
    sys.exit(main())
