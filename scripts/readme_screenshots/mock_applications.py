"""Add a mock application history to the demo DB, at invented firms only.

Usage: .venv/bin/python scripts/readme_screenshots/mock_applications.py /tmp/demo_jobs.db

Nothing here is the owner's: firms, roles, mail and caps are fictional, so the
application screens can be shown without describing anyone's real search.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jobfeed.db import JobDB  # noqa: E402

db = JobDB(sys.argv[1])
now = datetime.now(timezone.utc)
ago = lambda d, h=10: (now - timedelta(days=d, hours=-h)).isoformat()

#  id, firm, title, city, country, region, area, desk, job_type, status, applied days ago
ROLES = [
    ("mock_nw_gm", "Northwind Capital", "Global Markets Graduate Programme 2027", "London",
     "United Kingdom", "Europe", "markets", "trading", "graduate-programme", "interview", 34),
    ("mock_hd_sa", "Halden & Co", "2027 Summer Analyst – Sales & Trading", "New York",
     "United States", "Americas", "markets", "sales", "internship", "oa", 21),
    ("mock_td_gt", "Tidal Traders", "Graduate Trader", "Amsterdam", "Netherlands", "Europe",
     "markets", "trading", "graduate-programme", "offer", 58),
    ("mock_ks_qt", "Keystone Securities", "Quantitative Trader Intern", "Chicago",
     "United States", "Americas", "quant", "trading", "internship", "applied", 9),
    ("mock_rb_gm", "Rhein Bank", "Global Markets Graduate Programme", "Frankfurt", "Germany",
     "Europe", "markets", "sales", "graduate-programme", "rejected", 47),
    ("mock_cv_gt", "Corvid Securities", "Graduate Trader, Rates", "London", "United Kingdom",
     "Europe", "markets", "trading", "graduate-programme", "applied", 5),
    ("mock_vr_ct", "Veranta", "Commodities Trading Graduate", "Geneva", "Switzerland",
     "Europe", "markets", "trading", "graduate-programme", "oa", 16),
    ("mock_mg_si", "Marlow Group", "2027 Fixed Income Summer Internship", "Sydney", "Australia",
     "APAC", "markets", "sales", "internship", "rejected", 40),
    ("mock_ba_st", "Banque Aurel", "Off-cycle Internship, Structuring", "Paris", "France",
     "Europe", "markets", "structuring", "internship", "applied", 12),
    ("mock_hp_ad", "Harbor Point Trading", "Algorithm Developer Intern", "New York",
     "United States", "Americas", "quant", "strats", "internship", "queued", 0),
    ("mock_ar_gt", "Arden Bank", "Graduate Talent Program – Credit Solutions", "Zurich",
     "Switzerland", "Europe", "markets", "structuring", "graduate-programme", "applied", 27),
    ("mock_lp_qt", "Leap Trading", "Campus Quant Trader", "Singapore", "Singapore", "APAC",
     "quant", "trading", "graduate-programme", "interview", 30),
    ("mock_aw_ap", "Aldwych Bank", "Analyst Programme – Investment Banking", "London",
     "United Kingdom", "Europe", "ibd", "", "graduate-programme", "rejected", 63),
    ("mock_bw_gt", "Brightwater Trading", "Graduate Trader", "Hong Kong", "Hong Kong", "APAC",
     "markets", "trading", "graduate-programme", "applied", 3),
    ("mock_nr_eq", "Norvik Group", "Equity Research Summer Internship", "Milan", "Italy",
     "Europe", "research", "", "internship", "oa", 19),
    ("mock_sl_pe", "Solace Partners", "Private Equity Off-Cycle Intern", "Madrid", "Spain",
     "Europe", "private-markets", "", "internship", "applied", 24),
]
for (jid, firm, title, city, country, region, area, desk, jtype, status, days) in ROLES:
    slug = firm.lower().replace(" & ", "-").replace(" ", "-")
    db.mark_seen(jid, company=firm, title=title, url=f"https://careers.{slug}.example/jobs/{jid}",
                 category="Mock firm", location=f"{city}, {country}",
                 description=(f"{firm} is recruiting for its {title}. "
                              "This is a fictional posting used to illustrate the application "
                              "screens; the firm does not exist."))
    db.set_tags(jid, area=area, desk=desk, seniority="graduate" if jtype != "internship" else "intern",
                job_type=jtype, loc_city=city, loc_country=country, loc_region=region,
                work_mode="onsite", education="bachelor", min_yoe=0)
    if status != "new":
        db.set_status(jid, status)
    db.conn.execute("UPDATE seen_jobs SET first_seen=?, last_seen=?, applied_at=? WHERE id=?",
                    (ago(days + 6), ago(0), None if status == "queued" else ago(days), jid))
db.conn.execute("UPDATE seen_jobs SET favorite=1 WHERE id IN ('mock_hp_ad', 'mock_ks_qt', 'mock_nr_eq')")
db.conn.commit()

# Attended workflows in a few states.
for jid, steps in (("mock_hp_ad", []), ("mock_cv_gt", ["in_progress", "review_ready"]),
                   ("mock_bw_gt", ["in_progress", "needs_user_action"])):
    wf, _ = db.create_application_workflow(jid, f"https://careers.example/apply/{jid}")
    for st in steps:
        db.transition_application_workflow(wf["workflow_id"], st, actor="owner",
                                           detail="CAPTCHA before the final page"
                                           if st == "needs_user_action" else "")

# Inbox: a confirmation, an assessment invitation with a task, a rejection with
# a stated cool-down.
base = {"proposed_status": "", "evidence": "", "match_reason": "model matched",
        "action_required": "", "task_key": "", "action_url": "", "action_deadline": "",
        "rejection_reason": "", "reason_kind": "", "reapply_kind": "", "reapply_after": "",
        "reapply_quote": ""}
MAIL = [
    dict(base, message_key="m1", received_at=ago(3), sender="careers@brightwater.example",
         subject="Thank you for applying to Brightwater Trading", job_id="mock_bw_gt",
         proposed_status="applied", outcome="applied", evidence="We have received your application."),
    dict(base, message_key="m2", received_at=ago(2), sender="noreply@halden.example",
         subject="Halden & Co | Action required: complete your online assessment",
         job_id="mock_hd_sa", proposed_status="oa", outcome="applied",
         evidence="Please complete the online assessment within 5 days.",
         action_required="Complete the online assessment.", task_key="online-assessment",
         action_url="https://assess.example/halden", action_deadline=(now + timedelta(days=4)).date().isoformat()),
    dict(base, message_key="m3", received_at=ago(12), sender="recruiting@rheinbank.example",
         subject="Your application to Rhein Bank", job_id="mock_rb_gm",
         proposed_status="rejected", outcome="applied",
         evidence="We will not be progressing your application further.",
         rejection_reason="We have decided to move forward with other candidates.",
         reason_kind="comparative_fit", reapply_kind="fixed_duration",
         reapply_after=(now + timedelta(days=170)).date().isoformat(),
         reapply_quote="You may reapply after six months."),
    dict(base, message_key="m4", received_at=ago(1), sender="talent@veranta.example",
         subject="Veranta: invitation to the numerical test", job_id="mock_vr_ct",
         proposed_status="oa", outcome="applied", evidence="You are invited to complete our numerical test.",
         action_required="Complete the numerical test.", task_key="numerical-test",
         action_deadline=(now + timedelta(days=6)).date().isoformat()),
]
for m in MAIL:
    db.record_application_mail(m)

# Application caps at the mock firms, each with the sentence it came from.
for firm, n, cycle, quote in (
        ("Halden & Co", 3, "recruiting season", "You may submit up to three applications per recruiting season."),
        ("Northwind Capital", 1, "academic year", "Candidates may apply to one programme per academic year."),
        ("Rhein Bank", 2, "year", "We accept a maximum of two applications per year."),
        ("Arden Bank", 1, "academic year", "Only one application per programme and academic year will be considered.")):
    db.set_company_limit(firm, max_per_cycle=n, cycle=cycle, confidence="stated", strength="hard",
                         quote=quote, source_url=f"https://careers.example/{firm.split()[0].lower()}/faq")
db.conn.commit()
# Starred roles still to apply to, each with a closing date, so the deadline
# view has something to show.
t = now.date()
for jid, d in (("mock_hp_ad", 5), ("mock_ks_qt", 11), ("mock_nr_eq", 19), ("mock_cv_gt", 26)):
    db.set_deadline(jid, (t + timedelta(days=d)).isoformat())
for jid, firm, title, city, country, region, area, d in (
        ("mock_gl_fx", "Glenmoor Bank", "FX Sales Graduate Programme 2027", "London",
         "United Kingdom", "Europe", "markets", 9),
        ("mock_or_qr", "Orion Quant Partners", "Quantitative Researcher Intern", "New York",
         "United States", "Americas", "quant", 16),
        ("mock_ve_ec", "Vesper Capital", "Macro Research Analyst Programme", "Singapore",
         "Singapore", "APAC", "economics", 24)):
    db.mark_seen(jid, company=firm, title=title,
                 url=f"https://careers.{firm.split()[0].lower()}.example/{jid}",
                 category="Mock firm", location=f"{city}, {country}",
                 description=(f"{firm} is recruiting for its {title}. This is a fictional "
                              "posting used to illustrate the application screens; the firm "
                              "does not exist."))
    db.set_tags(jid, area=area, seniority="graduate", job_type="graduate-programme",
                loc_city=city, loc_country=country, loc_region=region, work_mode="onsite",
                education="bachelor", min_yoe=0)
    db.set_status(jid, "queued")
    db.set_deadline(jid, (t + timedelta(days=d)).isoformat())
    db.conn.execute("UPDATE seen_jobs SET favorite=1, first_seen=?, last_seen=? WHERE id=?",
                    (ago(12), ago(0), jid))

# Two attended runs that finished, so the Submitted tile is not empty.
for jid in ("mock_ks_qt", "mock_ba_st"):
    wf, _ = db.create_application_workflow(jid, f"https://careers.example/apply/{jid}")
    for st in ("in_progress", "review_ready", "completed"):
        db.transition_application_workflow(wf["workflow_id"], st, actor="owner")
db.conn.commit()
print("mock firms", len(ROLES) + 3, "workflows 5, mails", len(MAIL))
