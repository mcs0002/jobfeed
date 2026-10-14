"""Associate as the graduate entry title (tag._enforce_entry_associate).

Browse hides seniority='associate' by default. At the Big 4 and the strategy
consultancies "Associate" is the graduate entry hire, and on 2026-10-10 ~100
live entry roles were hidden by that default. Every example from that audit is
here, on the side it belongs.
"""
import csv
import glob
import os
import sys
import tempfile
import unittest
from unittest import mock

from jobfeed import tag
from jobfeed.db import JobDB

ENTRY = [  # (title, company, min_yoe): must become graduate
    ("Risk Services Associate (July 2027 Intake)", "PwC (Campus)", None),
    ("FDD Associate", "PwC (Campus)", 0),
    ("Associate", "PwC (Campus)", None),
    ("Asistentes Tax", "PwC (Campus)", None),
    ("Statistician", "PwC (Campus)", 0),
    ("Associate 2027, Belgium", "BCG", None),
    ("Kearney Associate 2026", "Kearney", 0),
    ("Audit Associate", "KPMG Spain", None),
    ("Tax Associate", "EY (Early Careers)", None),
    ("Associate Consultant", "Bain & Company", None),
    ("Graduate Associate Programme 2027", "Commerzbank (Campus)", None),
    ("Associate – September 2027 Intake", "Oliver Wyman", 2),
    # Refinement, 2026-10-10: McKinsey's entry titles, and explicit
    # new-graduate wording at any firm.
    ("Junior Associate", "McKinsey", None),
    ("Fellow - Munich", "McKinsey", None),
    ("Business Analyst", "McKinsey", None),
    ("Commercial Banking Associate Program – New Grad", "CIBC", None),
    ("Finance Rotation Program Associate (New Graduate Opportunity)", "BMO", None),
    ("Associate - Entry level position", "L.E.K. Consulting", None),
    ("[2027 Master's graduates] Consulting Associate", "Charles River Associates", None),
]
STAYS = [  # must keep seniority='associate' (hidden by default)
    ("Summer Associate", "Goldman Sachs (Campus)", None),
    ("2027 Summer Associate Program", "J.P. Morgan", None),
    ("Global Markets Summer Associate", "Bank of America (Campus)", None),
    ("JD/MBA Associate Program", "J.P. Morgan (Campus)", None),
    ("MBA Associate 2027", "BCG", None),
    ("PhD Associate", "McKinsey", None),
    ("2027 Private Equity Associate", "Blackstone", None),
    ("2027 Private Equity Associate", "Blackstone (Campus)", None),
    ("Experienced Associate, Deals", "PwC", None),
    ("Senior Associate", "PwC (Campus)", None),
    ("Sr. Associate, Valuations", "Deloitte", None),
    ("Associate Director", "KPMG", None),
    ("Associate", "PwC", 3),
    ("Associate", "Bain Capital", None),
    ("Associate, Investment Banking", "Lazard (Campus)", None),
    # Refinement, 2026-10-10: above-entry consulting titles at listed firms,
    # McKinsey's post-MBA Associate, and bank Junior Associates.
    ("Consultant or Project Leader, Risk and Compliance", "BCG", None),
    ("Consultant (all genders)", "Roland Berger", None),
    ("Consultant - Private Equity & Principals Investors", "McKinsey", None),
    ("Project Leader, Climate & Sustainability", "BCG", None),
    ("Associate - Tech & AI", "McKinsey", None),
    ("Associate - Private Capital", "McKinsey", None),
    ("Associate (m/w/d)", "McKinsey", None),
    ("Associate (m/w/d)", "McKinsey (Campus)", None),
    ("Junior Associate, Investment Banking - Industrials", "Morgan Stanley", None),
    ("Junior Associate, Global Advisory", "Rothschild & Co", None),
    ("Junior Associate", "Evercore", None),
    ("Junior Associate, Capital Markets", "BMO", None),
    ("Junior Associate, Investment Banking", "TD Securities", None),
    ("Banking Associate Graduate Program 2027 New York City/San Francisco",
     "Barclays", None),
]


def guarded(title, company, min_yoe, seniority="associate", job_type="job"):
    j = {"title": title, "company": company, "min_yoe": min_yoe,
         "seniority": seniority, "job_type": job_type}
    tag._enforce_entry_associate(j)
    return j["seniority"]


class GuardTests(unittest.TestCase):
    def test_entry_hires_become_graduate(self):
        for title, company, yoe in ENTRY:
            with self.subTest(title=title, company=company):
                self.assertEqual("graduate", guarded(title, company, yoe))

    def test_senior_and_mba_rows_stay_associate(self):
        for title, company, yoe in STAYS:
            with self.subTest(title=title, company=company):
                self.assertEqual("associate", guarded(title, company, yoe))

    def test_an_mba_track_named_only_in_the_description_stays(self):
        # Barclays US: the title reads like a graduate programme; the posting's
        # opening says it is for MBA graduates. Same title, outside the US and
        # without that sentence, is a real graduate programme.
        mba = ("Barclays Investment Bank's Banking Associate Graduate Program is a "
               "full-time program for MBA graduates joining as Associates.")
        j = {"title": "Banking Associate Graduate Program 2027", "company": "Barclays",
             "seniority": "associate", "job_type": "job", "description": mba}
        tag._enforce_entry_associate(j)
        self.assertEqual("associate", j["seniority"])
        j.update(seniority="associate", description="A two-year rotational programme.")
        tag._enforce_entry_associate(j)
        self.assertEqual("graduate", j["seniority"])

    def test_only_associate_rows_and_never_internships(self):
        self.assertEqual("analyst", guarded("Associate", "PwC", None, seniority="analyst"))
        self.assertEqual("manager", guarded("Associate", "PwC", None, seniority="manager"))
        self.assertEqual("associate", guarded("Summer Associate", "PwC", None,
                                              job_type="internship"))

    def test_firm_list_matches_whole_names_only(self):
        self.assertTrue(tag.entry_associate_firm("KPMG (US)"))
        self.assertTrue(tag.entry_associate_firm("EY (Early Careers)"))
        self.assertFalse(tag.entry_associate_firm("Bain Capital"))
        self.assertFalse(tag.entry_associate_firm("Walleye Capital"))
        self.assertFalse(tag.entry_associate_firm("EYB Partners"))

    def test_the_tagger_runs_it(self):
        jobs = [{"id": "x", "title": "FDD Associate", "company": "PwC (Campus)",
                 "location": "London", "description": ""}]

        def fake_batch(batch, *a, **k):
            for j in batch:
                j.update(area="consulting", seniority="associate", job_type="job")

        with mock.patch.object(tag, "_provider", return_value="cli"), \
                mock.patch.object(tag, "claude_bin", return_value="/bin/true"), \
                mock.patch.object(tag, "_tag_batch_any", side_effect=fake_batch), \
                mock.patch.object(tag, "_judge_batch", return_value=0), \
                mock.patch.object(tag, "_record_run"):
            tag.tag_jobs(jobs)
        self.assertEqual("graduate", jobs[0]["seniority"])


class ReapplyTests(unittest.TestCase):
    def test_dry_run_counts_and_apply_writes_an_undo_csv(self):
        from scripts import reapply_guards
        d = tempfile.mkdtemp()
        path = os.path.join(d, "jobs.db")
        db = JobDB(path)
        for i, (title, company, yoe) in enumerate(ENTRY + STAYS):
            db.mark_seen(f"r{i}", company=company, title=title, url=f"u{i}")
            db.set_tags(f"r{i}", area="consulting", seniority="associate", job_type="job",
                        min_yoe=yoe)
        db.conn.close()
        with mock.patch.object(sys, "argv", ["x", "--db", path, "--show", "0"]):
            self.assertEqual(0, reapply_guards.main())
        db = JobDB(path)
        self.assertEqual(len(ENTRY) + len(STAYS), db.conn.execute(
            "SELECT count(*) FROM seen_jobs WHERE seniority='associate'").fetchone()[0])
        db.conn.close()
        with mock.patch.object(sys, "argv", ["x", "--db", path, "--show", "0", "--apply"]):
            self.assertEqual(0, reapply_guards.main())
        db = JobDB(path)
        got = dict(db.conn.execute("SELECT title || '@' || company, seniority FROM seen_jobs"))
        for title, company, _ in ENTRY:
            self.assertEqual("graduate", got[f"{title}@{company}"], title)
        for title, company, _ in STAYS:
            self.assertEqual("associate", got[f"{title}@{company}"], title)
        undo = glob.glob(os.path.join(d, "reapply_guards_undo_*.csv"))
        self.assertEqual(1, len(undo))
        with open(undo[0]) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(ENTRY), len(rows))
        self.assertEqual({"associate"}, {r["seniority"] for r in rows})


if __name__ == "__main__":
    unittest.main()
