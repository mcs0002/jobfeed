"""Screening-answer bank: captured from attended runs, approved on /answers.

A capture is only ever a proposal; the export the browser agent reads holds
approved rows the current rules still allow, and nothing demographic, narrative
or profile-owned ever reaches it.
"""
import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WEB_PASSWORD", "owner-pw-test")
os.environ.setdefault("WEB_USER", "admin")
os.environ.setdefault("WEB_GUEST_PASSWORD", "guest-pw-test")
os.environ.setdefault("WEB_GUEST_USER", "guest")
os.environ.setdefault("WEB_SECRET", "application-workflow-test-secret")

from starlette.testclient import TestClient  # noqa: E402

import web.app as webapp  # noqa: E402
from applications import answers as bank  # noqa: E402
from applications import status as application_status  # noqa: E402
from applications.handoff import workflow_token  # noqa: E402
from jobfeed import db as dbmod  # noqa: E402
from jobfeed.db import JobDB  # noqa: E402

TRAVEL = "Are you willing to travel up to 20% of the time?*"
GENDER = "What is your gender identity?"
WHY = "Why do you want to work at Point72?"
VISA = "Will you now or in the future require visa sponsorship?"
PENULTIMATE = "Are you in the penultimate year of your degree?"


class ClassifierTests(unittest.TestCase):
    def test_key_ignores_case_punctuation_and_required_markers(self):
        self.assertEqual(bank.normalise_question(TRAVEL),
                         bank.normalise_question("are you willing to travel up to 20% of the time? (required)"))

    def test_categories_put_the_protective_rules_first(self):
        self.assertEqual(bank.SENSITIVE, bank.classify(GENDER))
        self.assertEqual(bank.SENSITIVE, bank.classify("Do you consider yourself to have a disability?"))
        self.assertEqual(bank.NARRATIVE, bank.classify(WHY))
        self.assertEqual(bank.NARRATIVE, bank.classify("Anything else?", "x" * 500))
        self.assertEqual(bank.PROFILE, bank.classify(VISA))
        self.assertEqual("eligibility", bank.classify(PENULTIMATE))
        self.assertEqual("logistics", bank.classify(TRAVEL))

    def test_only_reusable_classes_can_be_approved(self):
        for q in (GENDER, WHY, VISA):
            self.assertTrue(bank.approval_blocker(q, "Yes"), q)
        self.assertEqual("", bank.approval_blocker(TRAVEL, "Yes"))


class StatusRequestTests(unittest.TestCase):
    def _read(self, obj):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(obj, fh)
        try:
            return application_status._read_request(Path(fh.name))
        finally:
            os.unlink(fh.name)

    def test_answers_ride_the_same_fixed_file(self):
        req = self._read({"workflow_id": "apply_abc123", "status": "needs_user_action",
                          "answers": [{"question": TRAVEL, "answer": "Yes",
                                       "kind": "standing", "field": "Screening"}]})
        self.assertEqual("Yes", req["answers"][0]["answer"])

    def test_answers_need_a_reporting_state_and_their_shape(self):
        item = {"question": TRAVEL, "answer": "Yes"}
        with self.assertRaises(ValueError):
            self._read({"workflow_id": "apply_abc123", "status": "in_progress",
                        "answers": [item]})
        for bad in ([{**item, "state": "approved"}], [{"question": TRAVEL}],
                    [{**item, "kind": "forever"}], {"question": TRAVEL}):
            with self.assertRaises(ValueError):
                self._read({"workflow_id": "apply_abc123", "status": "review_ready",
                            "answers": bad})

    def test_reporter_forwards_answers_as_one_json_field(self):
        seen = {}

        class Resp:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(url, data=None, timeout=None):
            seen["body"] = data.decode()
            return Resp()

        with mock.patch.dict(os.environ, {"WEB_SECRET": "s", "WEB_PUBLIC_BASE_URL": "https://x"}), \
                mock.patch("urllib.request.urlopen", fake):
            application_status.report({
                "workflow_id": "apply_abc123", "status": "review_ready", "detail": "",
                "answers": [{"question": TRAVEL, "answer": "Yes", "kind": "standing",
                             "field": ""}]})
        self.assertIn("answers=", seen["body"])


class _WebCase(unittest.TestCase):
    def setUp(self):
        self._saved_auth = (webapp.WEB_PASSWORD, webapp.WEB_USER,
                            webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER)
        webapp.WEB_PASSWORD, webapp.WEB_USER = "owner-pw-test", "admin"
        webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER = "guest-pw-test", "guest"
        self._saved_db = webapp.DB_FILE
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        webapp.DB_FILE = self.path
        self.out = Path(tempfile.mkdtemp()) / "screening_answers.json"
        self._patch = mock.patch.object(bank, "APPROVED_FILE", self.out)
        self._patch.start()
        db = JobDB(self.path)
        db.mark_seen("p1", company="Point72 (Campus)", title="Academy",
                     url="https://x.test/p1")
        wf, _ = db.create_application_workflow(
            "p1", "https://point72.wd5.myworkdayjobs.com/x/job/1")
        db.transition_application_workflow(wf["workflow_id"], "in_progress", actor="agent")
        self.wid = wf["workflow_id"]
        db.conn.close()

    def tearDown(self):
        self._patch.stop()
        (webapp.WEB_PASSWORD, webapp.WEB_USER,
         webapp.WEB_GUEST_PASSWORD, webapp.WEB_GUEST_USER) = self._saved_auth
        webapp.DB_FILE = self._saved_db
        os.unlink(self.path)

    def db(self):
        return JobDB(self.path)

    def agent_report(self, status, answers):
        client = TestClient(webapp.app, base_url="https://testserver")
        return client.post(f"/application/{self.wid}/agent/status", data={
            "token": workflow_token(webapp.WEB_SECRET, self.wid), "status": status,
            "answers": json.dumps(answers)})

    def login(self, user, password):
        c = TestClient(webapp.app, base_url="https://testserver")
        c.post("/login", data={"username": user, "password": password},
               follow_redirects=False)
        return c

    def owner(self):
        c = self.login(webapp.WEB_USER, webapp.WEB_PASSWORD)
        self.assertEqual(200, c.get("/answers").status_code)
        return c

    @staticmethod
    def csrf(c):
        """The session's token, read off a page that has a form on it."""
        m = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/answers").text)
        return m.group(1) if m else ""


class CaptureTests(_WebCase):
    def test_captured_answers_land_as_proposals_only(self):
        r = self.agent_report("review_ready", [
            {"question": TRAVEL, "answer": "Yes", "kind": "standing", "field": "Screening"},
            {"question": "Do you hold a CFA charter?", "answer": "No",
             "kind": "per-application"},
            {"question": GENDER, "answer": "Man"},
            {"question": WHY, "answer": "Because."}])
        self.assertEqual(200, r.status_code)
        self.assertIn("answers proposed: 2, 2 not stored", r.headers["X-Jobfeed-Message"])
        rows = self.db().screening_answers()
        self.assertEqual({"proposed"}, {r["state"] for r in rows})
        self.assertNotIn(GENDER, {r["question"] for r in rows})
        travel = next(r for r in rows if r["question"] == TRAVEL)
        self.assertEqual(("", "Point72 (Campus)", "point72.wd5.myworkdayjobs.com", self.wid),
                         (travel["scope_firm"], travel["seen_company"],
                          travel["seen_ats"], travel["workflow_id"]))
        cfa = next(r for r in rows if r["question"].startswith("Do you hold"))
        self.assertEqual("Point72", cfa["scope_firm"])
        self.assertFalse(self.out.exists(), "a capture never writes the approved file")

    def test_a_bad_observation_never_blocks_the_status(self):
        r = self.agent_report("needs_user_action",
                              [{"question": TRAVEL, "answer": "Yes", "state": "approved"}])
        self.assertEqual(200, r.status_code)
        self.assertEqual("needs_user_action",
                         self.db().get_application_workflow(self.wid)["status"])
        self.assertEqual([], self.db().screening_answers())

    def test_the_same_answer_again_is_counted_not_queued_twice(self):
        for _ in range(2):
            self.agent_report("needs_user_action", [{"question": TRAVEL, "answer": "Yes"}])
        rows = self.db().screening_answers()
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["seen_count"])


class ApprovalTests(_WebCase):
    def _propose(self, question, answer, kind="standing"):
        self.agent_report("needs_user_action",
                          [{"question": question, "answer": answer, "kind": kind}])
        return next(r["id"] for r in self.db().screening_answers(state="proposed")
                    if r["question"] == question and r["answer"] == answer)

    def test_owner_approval_writes_the_file_and_replaces_the_old_answer(self):
        c = self.owner()
        first = self._propose(TRAVEL, "Yes")
        r = c.post(f"/answers/{first}/approve", data={"csrf_token": self.csrf(c)},
                   follow_redirects=False)
        self.assertEqual(303, r.status_code)
        exported = json.loads(self.out.read_text())["answers"]
        self.assertEqual([("Yes", "")], [(a["answer"], a["firm"]) for a in exported])
        self.assertEqual(0o600, self.out.stat().st_mode & 0o777)
        second = self._propose(TRAVEL, "No")
        c.post(f"/answers/{second}/approve", data={"csrf_token": self.csrf(c)})
        self.assertEqual("retired", self.db().get_screening_answer(first)["state"])
        self.assertEqual(["No"], [a["answer"] for a in
                                  json.loads(self.out.read_text())["answers"]])

    def test_firm_override_sits_beside_the_general_answer(self):
        c = self.owner()
        general = self._propose(TRAVEL, "Yes")
        override = self._propose(TRAVEL, "No", kind="per-application")
        for i in (general, override):
            c.post(f"/answers/{i}/approve", data={"csrf_token": self.csrf(c)})
        exported = json.loads(self.out.read_text())["answers"]
        self.assertEqual({("", "Yes"), ("Point72", "No")},
                         {(a["firm"], a["answer"]) for a in exported})

    def test_profile_facts_cannot_be_approved_or_edited_into_the_export(self):
        c = self.owner()
        visa = self._propose(VISA, "No")
        self.assertEqual("profile", self.db().get_screening_answer(visa)["category"])
        r = c.post(f"/answers/{visa}/approve", data={"csrf_token": self.csrf(c)})
        self.assertIn("applicant_profile.json", r.text)
        self.assertEqual("proposed", self.db().get_screening_answer(visa)["state"])
        # Recategorising does not help: the wording decides.
        c.post(f"/answers/{visa}/edit", data={"csrf_token": self.csrf(c), "answer": "No",
                                             "category": "other", "scope_firm": ""})
        c.post(f"/answers/{visa}/approve", data={"csrf_token": self.csrf(c)})
        self.assertEqual("proposed", self.db().get_screening_answer(visa)["state"])

    def test_edit_retire_and_search(self):
        c = self.owner()
        i = self._propose(TRAVEL, "Yes")
        c.post(f"/answers/{i}/edit", data={"csrf_token": self.csrf(c), "answer": "Yes, up to 30%",
                                          "category": "logistics", "scope_firm": "Citi (Campus)"})
        row = self.db().get_screening_answer(i)
        self.assertEqual(("Yes, up to 30%", "Citi"), (row["answer"], row["scope_firm"]))
        self.assertIn("Yes, up to 30%", c.get("/answers?q=travel").text)
        self.assertNotIn("Yes, up to 30%", c.get("/answers?category=consent").text)
        c.post(f"/answers/{i}/retire", data={"csrf_token": self.csrf(c)})
        self.assertEqual("retired", self.db().get_screening_answer(i)["state"])

    def test_writes_need_the_csrf_token(self):
        c = self.owner()
        i = self._propose(TRAVEL, "Yes")
        self.assertEqual(403, c.post(f"/answers/{i}/approve", data={}).status_code)
        self.assertEqual("proposed", self.db().get_screening_answer(i)["state"])


class GuestTests(_WebCase):
    def test_guest_is_denied_the_page_and_every_write(self):
        c = self.login(webapp.WEB_GUEST_USER, webapp.WEB_GUEST_PASSWORD)
        self.assertEqual(403, c.get("/answers").status_code)
        self.assertEqual(403, c.post("/answers/1/approve", data={}).status_code)
        self.assertNotIn('href="/answers"', c.get("/").text)

    def test_nav_item_is_owner_only_under_more(self):
        item = next(i for i in webapp.NAV_ITEMS if i["href"] == "/answers")
        self.assertEqual("more", item.get("group"))
        self.assertFalse(item.get("guest"))
        self.assertIn('href="/answers"', self.owner().get("/").text)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        self.db = JobDB(self.path)

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.path)

    def _row(self, question, answer, state, category=None):
        self.db.propose_screening_answer(
            question=question, question_key=bank.normalise_question(question),
            answer=answer, category=category or bank.classify(question, answer))
        rid = self.db.conn.execute("SELECT MAX(id) FROM screening_answers").fetchone()[0]
        if state != "proposed":
            # Straight to the table: the export must hold even against rows
            # that bypassed the approval gate (an older classifier, a hand edit).
            self.db.conn.execute("UPDATE screening_answers SET state=? WHERE id=?", (state, rid))
            self.db.conn.commit()

    def test_export_holds_only_approved_reusable_rows(self):
        self._row(TRAVEL, "Yes", "approved")
        self._row("Do you hold a CFA charter?", "No", "proposed")
        self._row("Have you got a driving licence?", "Yes", "retired")
        self._row(GENDER, "Man", "approved", category="other")
        self._row(VISA, "No", "approved", category="other")
        self._row(WHY, "Because.", "approved", category="other")
        self._row(PENULTIMATE, "No", "approved")
        out = Path(tempfile.mkdtemp()) / "screening_answers.json"
        bank.write_approved(self.db, out)
        rows = json.loads(out.read_text())["answers"]
        self.assertEqual({TRAVEL, PENULTIMATE}, {r["question"] for r in rows})
        self.assertTrue(next(r for r in rows if r["question"] == PENULTIMATE)["defer_to_rules"])
        self.assertFalse(next(r for r in rows if r["question"] == TRAVEL)["defer_to_rules"])

    def test_the_file_lives_where_the_public_export_never_reaches(self):
        rel = bank.APPROVED_FILE.relative_to(bank.ROOT)
        self.assertEqual("secrets", rel.parts[0])
        script = (bank.ROOT / "scripts" / "export_public.sh").read_text()
        self.assertIn("--exclude='secrets'", script)


class MigrationTests(unittest.TestCase):
    def test_an_existing_database_gains_the_table_and_keeps_its_rows(self):
        path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        try:
            db = JobDB(path)
            db.mark_seen("old1", company="Old", title="Analyst", url="https://x.test/1")
            db.conn.execute("DROP TABLE screening_answers")
            db.conn.commit()
            db.conn.close()
            dbmod._SCHEMA_READY.discard(os.path.abspath(path))
            db = JobDB(path)
            tables = {r[0] for r in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("screening_answers", tables)
            self.assertIsNotNone(db.get_job("old1"))
            seen_cols = {r[1] for r in db.conn.execute("PRAGMA table_info(seen_jobs)")}
            self.assertFalse({"answer", "question_key"} & seen_cols)
            db.propose_screening_answer(question=TRAVEL, question_key="k",
                                        answer="Yes", category="logistics")
            with self.assertRaises(sqlite3.IntegrityError):
                db.conn.execute("UPDATE screening_answers SET state='approved'")
                db.conn.execute(
                    "INSERT INTO screening_answers (question_key, question, answer, "
                    "state, created_at, updated_at) VALUES ('k', 'q', 'No', 'approved', '', '')")
            db.conn.close()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
