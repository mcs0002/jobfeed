"""Durable attended-application queue and website handoff contracts."""
import html
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


os.environ["WEB_PASSWORD"] = "owner-pw-test"
os.environ["WEB_USER"] = "admin"
os.environ["WEB_GUEST_PASSWORD"] = "guest-pw-test"
os.environ["WEB_GUEST_USER"] = "guest"
os.environ["WEB_SECRET"] = "application-workflow-test-secret"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from applications.handoff import workflow_token
from jobfeed.db import JobDB  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
import web.app as webapp  # noqa: E402
from applications import launcher  # noqa: E402


ROLE_URL = "https://apply.example.test/jobs/real-role?source=jobfeed"


def seed(path: str) -> None:
    db = JobDB(path)
    db.conn.execute(
        "INSERT INTO seen_jobs "
        "(id, company, title, url, first_seen, last_seen, status) VALUES "
        "('j1', 'TestCo', 'Markets Analyst', ?, '2026-09-12', '2026-09-12', 'new'),"
        "('bad', 'BadCo', 'Unsafe URL', 'javascript:alert(1)', '2026-09-12', '2026-09-12', 'new')",
        (ROLE_URL,),
    )
    db.conn.commit()
    db.conn.close()


class WorkflowDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        seed(self.tmp.name)
        self.db = JobDB(self.tmp.name)

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.tmp.name)

    def test_schema_and_create_are_idempotent_and_snapshot_url(self):
        tables = {r[0] for r in self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn("application_workflows", tables)
        first, created = self.db.create_application_workflow(
            "j1", ROLE_URL, workflow_id="apply_test"
        )
        self.assertTrue(created)
        self.db.conn.execute(
            "UPDATE seen_jobs SET url='https://changed.invalid' WHERE id='j1'"
        )
        second, created_again = self.db.create_application_workflow(
            "j1", "https://changed.invalid", workflow_id="apply_other"
        )
        self.assertFalse(created_again)
        self.assertEqual(first["workflow_id"], second["workflow_id"])
        self.assertEqual(ROLE_URL, second["application_url"])
        self.assertEqual(1, self.db.conn.execute(
            "SELECT COUNT(*) FROM application_workflows WHERE job_id='j1'"
        ).fetchone()[0])

    def test_workflow_list_is_strictly_most_recent_update_first(self):
        self.db.conn.execute(
            "INSERT INTO seen_jobs "
            "(id, company, title, url, first_seen, last_seen, status) VALUES "
            "('j2', 'RecentCo', 'Recent role', 'https://apply.example.test/j2', "
            "'2026-09-14', '2026-09-14', 'new')"
        )
        older, _ = self.db.create_application_workflow(
            "j1", ROLE_URL, workflow_id="apply_older")
        newer, _ = self.db.create_application_workflow(
            "j2", "https://apply.example.test/j2", workflow_id="apply_newer")
        self.db.conn.execute(
            "UPDATE application_workflows SET status='needs_user_action', "
            "updated_at='2026-09-13T20:00:00+00:00' WHERE workflow_id=?",
            (older["workflow_id"],),
        )
        self.db.conn.execute(
            "UPDATE application_workflows SET status='failed', "
            "updated_at='2026-09-14T09:00:00+00:00' WHERE workflow_id=?",
            (newer["workflow_id"],),
        )
        self.db.conn.commit()

        rows = self.db.list_application_workflows()

        self.assertEqual(["apply_newer", "apply_older"],
                         [row["workflow_id"] for row in rows])

    def test_employer_confirmation_closes_the_run_without_regressing_the_crm(self):
        db = JobDB(self.tmp.name)
        try:
            workflow, _ = db.create_application_workflow("j1", ROLE_URL)
            wid = workflow["workflow_id"]
            db.transition_application_workflow(wid, "in_progress", actor="agent")
            db.transition_application_workflow(wid, "review_ready", actor="agent")
            db.set_status("j1", "oa")
            self.assertTrue(db.confirm_application_submission(
                "j1", "2026-09-12T20:35:03+00:00", "Thanks for applying"))
            closed = db.get_application_workflow(wid)
            self.assertEqual(("completed", "employer"), (closed["status"], closed["updated_by"]))
            self.assertEqual("oa", db.get_job("j1")["status"])
            with self.assertRaises(PermissionError):
                db.transition_application_workflow(wid, "in_progress", actor="employer")
        finally:
            db.conn.close()

    def test_backfill_closes_runs_stamped_before_the_rule_existed(self):
        db = JobDB(self.tmp.name)
        try:
            workflow, _ = db.create_application_workflow("j1", ROLE_URL)
            db.conn.execute("UPDATE application_workflows SET submitted_confirmed_at='2026-09-12' "
                            "WHERE workflow_id=?", (workflow["workflow_id"],))
            db.conn.commit()
            db.backfill_submission_confirmations()
            self.assertEqual("completed",
                             db.get_application_workflow(workflow["workflow_id"])["status"])
        finally:
            db.conn.close()

    def test_prior_applications_reach_the_launch_prompt(self):
        from applications.handoff import prior_applications_note
        self.assertIn("first application to the firm is Yes", prior_applications_note([]))
        note = prior_applications_note([{"title": "STS Graduate Singapore", "status": "oa",
                                         "applied_at": "2026-09-13T19:47:28+00:00"}])
        self.assertIn("STS Graduate Singapore (oa, applied 2026-09-13)", note)
        self.assertIn("NOT his first application", note)
        self.assertIn("never tick", note)

    def test_offer_elsewhere_is_live_and_ignores_the_same_firm(self):
        from applications.handoff import handoff_view
        db = JobDB(self.tmp.name)
        try:
            workflow, _ = db.create_application_workflow("j1", ROLE_URL)
            self.assertFalse(db.offer_elsewhere("j1"))
            db.conn.execute("INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen, status) "
                            "VALUES ('same', 'TestCo (Campus)', 'Other', 'https://x.test/s', '2026-09-13', '2026-09-13', 'offer')")
            self.assertFalse(db.offer_elsewhere("j1"), "an offer at the same firm is not elsewhere")
            db.conn.execute("INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen, status) "
                            "VALUES ('o1', 'OtherCo', 'Trader', 'https://x.test/o', '2026-09-13', '2026-09-13', 'offer')")
            self.assertTrue(db.offer_elsewhere("j1"))
            view = handoff_view(workflow, "s", "https://jobfeed.test", offer_elsewhere=True)
            self.assertTrue(view["handoff_messages"][1].startswith(
                f"Use /assisted-apply for workflow {workflow['workflow_id']}."))
            self.assertIn("holds an offer at another firm", view["primary_prompt"])
            self.assertIn("carries no new instruction", view["handoff_messages"][1])
        finally:
            db.conn.close()

    def test_owner_can_close_a_workflow_he_finished_by_hand(self):
        """A bp run stopped at needs_user_action, he submitted it himself, and
        the state machine had no edge for saying so."""
        db = JobDB(self.tmp.name)
        try:
            workflow, _ = db.create_application_workflow("j1", ROLE_URL)
            wid = workflow["workflow_id"]
            db.transition_application_workflow(wid, "in_progress", actor="agent")
            db.transition_application_workflow(wid, "needs_user_action", actor="agent")
            closed = db.transition_application_workflow(
                wid, "completed", detail="submitted by hand", actor="owner")
            self.assertEqual("completed", closed["status"])
            self.assertTrue(closed["completed_at"])
            self.assertEqual("applied", db.get_job("j1")["status"])
        finally:
            db.conn.close()

    def test_the_agent_still_cannot_close_one(self):
        db = JobDB(self.tmp.name)
        try:
            workflow, _ = db.create_application_workflow("j1", ROLE_URL)
            wid = workflow["workflow_id"]
            db.transition_application_workflow(wid, "in_progress", actor="agent")
            for state in ("in_progress", "needs_user_action", "queued", "failed"):
                with self.assertRaises(PermissionError):
                    db.transition_application_workflow(wid, "completed", actor="agent")
        finally:
            db.conn.close()

    def test_allowed_and_forbidden_transitions_and_no_submit_boundary(self):
        workflow, _ = self.db.create_application_workflow(
            "j1", ROLE_URL, workflow_id="apply_test"
        )
        wid = workflow["workflow_id"]
        running = self.db.transition_application_workflow(
            wid, "in_progress", detail="form open", actor="agent"
        )
        self.assertEqual("in_progress", running["status"])
        blocked = self.db.transition_application_workflow(
            wid, "needs_user_action", detail="Authentication required", actor="agent"
        )
        self.assertEqual("Authentication required", blocked["detail"])
        self.db.transition_application_workflow(wid, "in_progress", actor="agent")
        ready = self.db.transition_application_workflow(wid, "review_ready", actor="agent")
        self.assertEqual("review_ready", ready["status"])
        with self.assertRaises(PermissionError):
            self.db.transition_application_workflow(wid, "completed", actor="agent")
        done = self.db.transition_application_workflow(wid, "completed", actor="owner")
        self.assertEqual("completed", done["status"])
        job = self.db.get_job("j1")
        self.assertEqual("applied", job["status"])
        self.assertIsNotNone(job["applied_at"])
        with self.assertRaises(ValueError):
            self.db.transition_application_workflow(wid, "in_progress", actor="owner")

    def test_launch_state_is_idempotent_single_flight_and_recoverable(self):
        one, _ = self.db.create_application_workflow("j1", ROLE_URL, workflow_id="apply_12345678")
        queued, created = self.db.request_application_launch(one["workflow_id"])
        self.assertTrue(created)
        duplicate, created = self.db.request_application_launch(one["workflow_id"])
        self.assertFalse(created)
        self.assertEqual(queued["launch_request_id"], duplicate["launch_request_id"])
        claimed = self.db.claim_application_launch(queued["launch_request_id"])
        self.assertEqual("running", claimed["launch_status"])
        done = self.db.finish_application_launch(queued["launch_request_id"], False, "accessibility_denied")
        self.assertEqual(("failed", "accessibility_denied"),
                         (done["launch_status"], done["launch_error_code"]))
        retried, created = self.db.request_application_launch(one["workflow_id"], retry=True)
        self.assertTrue(created)
        self.assertNotEqual(queued["launch_request_id"], retried["launch_request_id"])

    def test_launcher_spool_is_private_and_dry_run_has_exact_two_messages(self):
        workflow, _ = self.db.create_application_workflow(
            "j1", ROLE_URL, workflow_id="apply_12345678"
        )
        run, _ = self.db.queue_application_run(workflow["workflow_id"], manual=True)
        self.db.acquire_application_run(run["run_id"])
        workflow, _ = self.db.request_application_launch(workflow["workflow_id"])
        with tempfile.TemporaryDirectory() as temp:
            old_support, old_queue = launcher.SUPPORT, launcher.QUEUE
            launcher.SUPPORT = launcher.Path(temp) / "support"
            launcher.QUEUE = launcher.SUPPORT / "queue"
            try:
                path = launcher.enqueue_launch(workflow)
                self.assertEqual(0o600, path.stat().st_mode & 0o777)
                self.assertEqual(0o700, launcher.QUEUE.stat().st_mode & 0o777)
                with mock.patch.dict(os.environ, {
                    "WEB_SECRET": "application-workflow-test-secret",
                    "WEB_PUBLIC_BASE_URL": "https://jobfeed.test",
                    "JOBS_DB": self.tmp.name,
                }):
                    payload = launcher._payload(workflow)
                self.assertEqual(2, len(payload["messages"]))
                # Antigravity delivers a queued message only after the running
                # turn ends, so the opening message has to carry the workflow
                # context itself, not rely on the second one arriving in time.
                opening = payload["messages"][0]
                self.assertTrue(opening.startswith(f"/browser {ROLE_URL}\n"))
                self.assertIn("apply_12345678", opening)
                self.assertIn(run["run_id"], opening)
                self.assertIn(f'"run_id": "{run["run_id"]}"', opening)
                self.assertIn("Never submit the application", opening)
                self.assertTrue(payload["messages"][1].startswith(
                    "Use /assisted-apply for workflow apply_12345678."
                ))
                self.assertNotIn("application-workflow-test-secret", json.dumps(payload))
            finally:
                launcher.SUPPORT, launcher.QUEUE = old_support, old_queue


class WorkflowEfficiencyTests(unittest.TestCase):
    def test_metrics_use_agent_start_to_review_and_exclude_open_runs(self):
        now = webapp.datetime.fromisoformat("2026-09-14T12:00:00+00:00")
        workflows = [
            {"status": "completed", "started_at": "2026-09-13T10:00:00+00:00",
             "review_ready_at": "2026-09-13T10:10:00+00:00", "updated_at": "2026-09-13T11:00:00+00:00"},
            {"status": "review_ready", "started_at": "2026-09-10T10:00:00+00:00",
             "review_ready_at": "2026-09-10T10:30:00+00:00", "updated_at": "2026-09-10T10:30:00+00:00"},
            {"status": "completed", "started_at": "2026-08-01T10:00:00+00:00",
             "review_ready_at": "2026-08-01T10:20:00+00:00", "updated_at": "2026-08-02T10:00:00+00:00"},
            {"status": "failed", "started_at": "2026-09-12T10:00:00+00:00",
             "review_ready_at": None, "updated_at": "2026-09-12T10:05:00+00:00"},
            {"status": "needs_user_action", "started_at": "2026-09-11T10:00:00+00:00",
             "review_ready_at": None, "updated_at": "2026-09-11T10:40:00+00:00"},
        ]
        metrics = webapp._application_efficiency(workflows, now=now)
        self.assertEqual(
            {"reached_review": 2, "median": "20m", "fastest": "10m",
             "failure_rate": "33%", "resolved": 3},
            metrics["recent"],
        )
        self.assertEqual(
            {"reached_review": 3, "median": "20m", "fastest": "10m",
             "failure_rate": "25%", "resolved": 4},
            metrics["all"],
        )

    def test_action_urgency_handles_due_and_missing_dates(self):
        now = webapp.datetime.fromisoformat("2026-09-14T12:00:00+00:00")
        tomorrow = webapp._application_action_view(
            {"action_deadline": "2026-09-15"}, now=now)
        missing = webapp._application_action_view({"action_deadline": ""}, now=now)
        self.assertEqual(("Due tomorrow", "urgent", "2026-09-15"),
                         (tomorrow["due_label"], tomorrow["due_tone"],
                          tomorrow["due_display"]))
        self.assertEqual(("No due date", "none", ""),
                         (missing["due_label"], missing["due_tone"],
                          missing["due_display"]))


class WorkflowWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        seed(self.tmp.name)
        self.old_db_file = webapp.DB_FILE
        webapp.DB_FILE = self.tmp.name
        self.old_public_base = os.environ.get("WEB_PUBLIC_BASE_URL")
        os.environ["WEB_PUBLIC_BASE_URL"] = "https://jobfeed.test"
        # The launch route ticks the autopilot, which reads this machine's real
        # Antigravity records; tests must not depend on a live agent.
        runs_patch = mock.patch.object(webapp, "_agent_runs", return_value=[])
        runs_patch.start()
        self.addCleanup(runs_patch.stop)

    def tearDown(self):
        webapp.DB_FILE = self.old_db_file
        if self.old_public_base is None:
            os.environ.pop("WEB_PUBLIC_BASE_URL", None)
        else:
            os.environ["WEB_PUBLIC_BASE_URL"] = self.old_public_base
        os.unlink(self.tmp.name)

    def client(self):
        return TestClient(webapp.app, base_url="https://jobfeed.test")

    @staticmethod
    def login_owner(client):
        response = client.post(
            "/login", data={"username": "admin", "password": "owner-pw-test"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    @staticmethod
    def login_guest(client):
        response = client.post(
            "/login", data={"username": "guest", "password": "guest-pw-test"},
            follow_redirects=False,
        )
        assert response.status_code == 303

    def _queue_mail(self, **over):
        db = JobDB(self.tmp.name)
        event = {
            "message_key": "mk1", "received_at": "2026-09-12T13:32:03+00:00",
            "sender": "no-reply@flowtraders.com",
            "subject": "Thank you for applying to Flow Traders",
            "job_id": "", "proposed_status": "applied", "outcome": "unmatched",
            "evidence": "Thank you for applying", "match_reason": "no candidate matched",
        }
        event.update(over)
        db.record_application_mail(event)
        db.conn.close()

    def _waiting(self, key, status="applied"):
        self._queue_mail(message_key=key, proposed_status=status,
                         sender="recruiting@testco.test",
                         subject=f"TestCo update {key}",
                         match_reason="model found no role; untracked role at TestCo")

    def _status_of(self, job_id="j1"):
        db = JobDB(self.tmp.name)
        try:
            return db.get_job(job_id)["status"], db.application_mail_for_job(job_id)
        finally:
            db.conn.close()

    def test_waiting_firm_mail_is_attached_from_the_role_pane(self):
        """DRW and Goldman on 2026-09-17: two open roles at each firm and a
        mail naming neither, so the mail waited on /applications while he
        worked on the role. It is now listed on the role and attached there."""
        self._waiting("w1")
        client = self.client()
        self.login_owner(client)
        pane = client.get("/job/j1?pane=1").text
        self.assertIn("Attach to this role", pane)
        self.assertIn("TestCo update w1", pane)
        token = re.search(r'name="csrf_token" value="([A-Za-z0-9_-]+)"', pane).group(1)
        card = client.post("/application-mail/w1/adopt",
                           data={"csrf_token": token, "job_id": "j1"},
                           headers={"HX-Request": "true"})
        self.assertEqual(200, card.status_code)
        self.assertIn('id="inbox-j1"', card.text)
        self.assertNotIn("Attach to this role", card.text)
        status, inbox = self._status_of()
        self.assertEqual("applied", status)
        self.assertEqual(["w1"], [m["subject"][-2:] for m in inbox])

    def test_a_reloaded_browse_page_shows_the_same_pane_as_a_click(self):
        """Browse draws the selected role into the page itself. On 2026-09-17
        that render had no limit, inbox or waiting mail, and no CSRF token for
        Attach, while clicking the same role showed all of them."""
        self._waiting("w1")
        client = self.client()
        self.login_owner(client)
        page = client.get("/?sel=j1").text
        self.assertIn("Application limit", page)
        self.assertIn("Attach to this role", page)
        token = re.search(r'name="csrf_token" value="([A-Za-z0-9_-]+)"', page)
        self.assertIsNotNone(token)

    def test_a_manual_status_attaches_the_one_waiting_mail_that_matches(self):
        self._waiting("w1", "applied")
        client = self.client()
        self.login_owner(client)
        client.post("/job/j1/status", data={"status": "applied"})
        status, inbox = self._status_of()
        self.assertEqual(("applied", 1), (status, len(inbox)))

    def test_a_manual_status_never_guesses_between_two_waiting_mails(self):
        self._waiting("w1", "applied")
        self._waiting("w2", "applied")
        self._waiting("w3", "oa")
        client = self.client()
        self.login_owner(client)
        client.post("/job/j1/status", data={"status": "applied"})
        self.assertEqual([], self._status_of()[1])
        client.post("/job/j1/status", data={"status": "interview"})
        self.assertEqual([], self._status_of()[1])

    def test_queued_mail_is_adopted_onto_a_role_the_owner_picks(self):
        self._queue_mail()
        client = self.client()
        self.login_owner(client)
        page = client.get("/applications")
        self.assertEqual(200, page.status_code)
        token = re.search(r'name="csrf_token" value="([A-Za-z0-9_-]+)"', page.text).group(1)
        response = client.post("/application-mail/mk1/adopt",
                               data={"csrf_token": token, "job_id": "j1"},
                               follow_redirects=False)
        self.assertEqual(303, response.status_code)
        db = JobDB(self.tmp.name)
        try:
            self.assertEqual("applied", db.get_job("j1")["status"])
            self.assertEqual([], db.pending_application_mail())
        finally:
            db.conn.close()

    def test_placing_a_mail_keeps_its_task_on_the_to_do_list(self):
        self._queue_mail(proposed_status="oa", subject="Complete Your Technical Assessment",
                         action_required="Complete the HackerRank challenge.",
                         task_key="online-assessment", action_deadline="2026-09-24")
        client = self.client()
        self.login_owner(client)
        token = re.search(r'name="csrf_token" value="([A-Za-z0-9_-]+)"',
                          client.get("/applications").text).group(1)
        client.post("/application-mail/mk1/adopt",
                    data={"csrf_token": token, "job_id": "j1"}, follow_redirects=False)
        db = JobDB(self.tmp.name)
        try:
            self.assertEqual([], db.pending_application_mail())
            self.assertEqual(["mk1"], [a["message_key"] for a in db.open_application_actions()])
        finally:
            db.conn.close()

    def test_adoption_takes_the_status_from_the_message_not_the_form(self):
        self._queue_mail(proposed_status="oa")
        client = self.client()
        self.login_owner(client)
        page = client.get("/applications")
        token = re.search(r'name="csrf_token" value="([A-Za-z0-9_-]+)"', page.text).group(1)
        # A crafted extra field must not choose the state, and is refused outright.
        self.assertEqual(403, client.post(
            "/application-mail/mk1/adopt",
            data={"csrf_token": token, "job_id": "j1", "status": "offer"},
            follow_redirects=False).status_code)
        client.post("/application-mail/mk1/adopt",
                    data={"csrf_token": token, "job_id": "j1"},
                    follow_redirects=False)
        db = JobDB(self.tmp.name)
        try:
            self.assertEqual("oa", db.get_job("j1")["status"])
        finally:
            db.conn.close()

    def test_adoption_needs_owner_and_csrf(self):
        self._queue_mail()
        anon = self.client()
        self.assertIn(anon.post("/application-mail/mk1/adopt",
                                data={"csrf_token": "x", "job_id": "j1"},
                                follow_redirects=False).status_code, (303, 401, 403))
        client = self.client()
        self.login_owner(client)
        self.assertEqual(403, client.post(
            "/application-mail/mk1/adopt",
            data={"csrf_token": "wrong", "job_id": "j1"},
            follow_redirects=False).status_code)
        db = JobDB(self.tmp.name)
        try:
            self.assertNotEqual("applied", db.get_job("j1")["status"])
        finally:
            db.conn.close()

    def test_adoption_never_drags_a_role_backwards(self):
        # Optiver sent an assessment invitation and a plain thank-you. Adopting
        # the second onto a role already at `oa` must not regress it.
        db = JobDB(self.tmp.name)
        db.set_status("j1", "oa")
        db.conn.close()
        self._queue_mail(proposed_status="applied")
        client = self.client()
        self.login_owner(client)
        page = client.get("/applications")
        token = re.search(r'name="csrf_token" value="([A-Za-z0-9_-]+)"', page.text).group(1)
        self.assertEqual(303, client.post(
            "/application-mail/mk1/adopt",
            data={"csrf_token": token, "job_id": "j1"},
            follow_redirects=False).status_code)
        db = JobDB(self.tmp.name)
        try:
            self.assertEqual("oa", db.get_job("j1")["status"])
            # The message is still resolved and its evidence still kept.
            self.assertEqual([], db.pending_application_mail())
            self.assertEqual(1, len(db.application_mail_for_job("j1")))
        finally:
            db.conn.close()

    def test_dismissing_queued_mail_changes_no_role(self):
        self._queue_mail()
        client = self.client()
        self.login_owner(client)
        page = client.get("/applications")
        token = re.search(r'name="csrf_token" value="([A-Za-z0-9_-]+)"', page.text).group(1)
        self.assertEqual(303, client.post("/application-mail/mk1/dismiss",
                                          data={"csrf_token": token},
                                          follow_redirects=False).status_code)
        db = JobDB(self.tmp.name)
        try:
            self.assertEqual([], db.pending_application_mail())
            self.assertNotEqual("applied", db.get_job("j1")["status"])
        finally:
            db.conn.close()

    def test_applications_top_metrics_and_deadline_first_actions_render(self):
        db = JobDB(self.tmp.name)
        workflow, _ = db.create_application_workflow("j1", ROLE_URL)
        now = webapp.datetime.now(webapp.timezone.utc)
        started = (now - webapp.timedelta(minutes=20)).isoformat()
        ready = (now - webapp.timedelta(minutes=5)).isoformat()
        db.conn.execute(
            "UPDATE application_workflows SET status='review_ready', started_at=?, "
            "review_ready_at=?, updated_at=? WHERE workflow_id=?",
            (started, ready, ready, workflow["workflow_id"]),
        )
        db.conn.commit()
        db.conn.close()
        tomorrow = (now.date() + webapp.timedelta(days=1)).isoformat()
        self._queue_mail(
            message_key="due", job_id="j1", proposed_status="oa",
            action_required="Complete the assessment.", task_key="assessment",
            action_url="https://tasks.example.test/assessment", action_deadline=tomorrow,
        )
        self._queue_mail(
            message_key="undated", job_id="j1", proposed_status="oa",
            subject="Upload supporting document", action_required="Upload the document.",
            task_key="document", action_url="", action_deadline="",
        )

        client = self.client()
        self.login_owner(client)
        page = client.get("/applications")
        self.assertEqual(200, page.status_code)
        body = html.unescape(page.text)
        self.assertNotIn("Website queue, Antigravity handoff, and review state", body)
        self.assertNotIn("Mail that asks you to do something", body)
        # One headline row since 2026-09-24; the rest fold under "All agent metrics".
        self.assertIn("Agent runs", body)
        self.assertIn("Reached review", body)
        self.assertIn("15m", body)
        # Inbox left, To do right, both above the application list (2026-09-17).
        self.assertLess(body.index("Agent runs"), body.index('aria-label="Inbox"'))
        self.assertLess(body.index('aria-label="Inbox"'), body.index('aria-label="To do"'))
        self.assertIn('class="apps-split"', body)
        self.assertLess(body.index("Due tomorrow"), body.index("No due date"))
        self.assertIn('class="todo-open" href="https://tasks.example.test/assessment"', body)
        self.assertIn("No direct link", body)
        self.assertIn('action="/application-mail/due/dismiss"', body)

    def test_workflow_rows_show_context_and_hide_misleading_review_link(self):
        db = JobDB(self.tmp.name)
        workflow, _ = db.create_application_workflow("j1", ROLE_URL)
        db.conn.execute(
            "UPDATE application_workflows SET status='review_ready', "
            "started_at='2026-09-14T10:00:00+00:00', "
            "review_ready_at='2026-09-14T10:12:00+00:00' WHERE workflow_id=?",
            (workflow["workflow_id"],),
        )
        db.conn.execute("UPDATE seen_jobs SET location='London, GB' WHERE id='j1'")
        db.conn.commit()
        db.conn.close()
        client = self.client()
        self.login_owner(client)
        body = html.unescape(client.get("/applications").text)
        self.assertIn('class="application-summary"', body)
        self.assertIn('class="application-details"', body)
        self.assertNotIn('class="application-details" open', body)
        self.assertIn("<summary>Details</summary>", body)
        self.assertNotIn("Resume review", body)
        self.assertIn('class="application-secondary" href="/job/j1">Open role</a>', body)
        self.assertIn('class="application-context-label">Location</span>', body)
        self.assertIn('class="application-context-value">London, GB</span>', body)
        self.assertIn('class="application-context-label">Updated</span>', body)
        # Timeline content remains present inside the collapsed disclosure.
        self.assertIn("agent started", body)
        self.assertIn("review ready", body)

        db = JobDB(self.tmp.name)
        db.conn.execute(
            "UPDATE application_workflows SET application_url='javascript:alert(1)'"
        )
        db.conn.commit()
        db.conn.close()
        unsafe = html.unescape(client.get("/applications").text)
        self.assertNotIn("Resume review", unsafe)
        self.assertIn("Open role", unsafe)

    def test_owner_handoff_propagates_real_url_and_duplicate_is_one_workflow(self):
        client = self.client()
        self.login_owner(client)
        detail = client.get("/job/j1?pane=1").text
        self.assertIn("Auto apply", detail)
        # Queue creation must remain a local DB operation, even when the stored
        # target is an external HTTPS URL.
        with mock.patch("socket.create_connection", side_effect=AssertionError("network used")):
            first = client.post("/job/j1/apply")
            second = client.post("/job/j1/apply")
        self.assertEqual(200, first.status_code)
        self.assertEqual(first.text, second.text)
        self.assertIn('"crm_status": "queued"', first.headers["hx-trigger"])
        body = html.unescape(first.text)
        self.assertIn(f"/browser {ROLE_URL}", body)
        self.assertIn("Copy opening message", body)
        self.assertIn("Never submit the application or mark it completed", body)
        self.assertNotIn("launched automatically", body)
        con = sqlite3.connect(self.tmp.name)
        count, url, status = con.execute(
            "SELECT COUNT(*), application_url, status FROM application_workflows"
        ).fetchone()
        crm = con.execute("SELECT status FROM seen_jobs WHERE id='j1'").fetchone()[0]
        con.close()
        self.assertEqual((1, ROLE_URL, "queued"), (count, url, status))
        self.assertEqual("queued", crm)

    def test_launch_requires_owner_csrf_and_refuses_extra_input(self):
        client = self.client()
        self.login_owner(client)
        panel = client.post("/job/j1/apply")
        workflow_id = re.search(r"/application/(apply_[a-f0-9]+)/launch", panel.text).group(1)
        token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', panel.text).group(1))
        self.assertEqual(403, client.post(
            f"/application/{workflow_id}/launch", data={"csrf_token": "wrong"}
        ).status_code)
        self.assertEqual(403, client.post(
            f"/application/{workflow_id}/launch",
            data={"csrf_token": token, "url": "https://attacker.invalid"},
        ).status_code)
        with mock.patch.object(webapp, "enqueue_launch") as enqueue:
            response = client.post(
                f"/application/{workflow_id}/launch", data={"csrf_token": token}
            )
        self.assertEqual(200, response.status_code)
        enqueue.assert_called_once()
        sent = enqueue.call_args.args[0]
        self.assertEqual(ROLE_URL, sent["application_url"])
        self.assertNotIn("attacker.invalid", response.text)
        with mock.patch.object(webapp, "enqueue_launch") as enqueue_again:
            again = client.post(
                f"/application/{workflow_id}/launch", data={"csrf_token": token}
            )
        self.assertEqual(200, again.status_code)
        enqueue_again.assert_not_called()

        guest = self.client(); self.login_guest(guest)
        self.assertEqual(403, guest.post(
            f"/application/{workflow_id}/launch", data={"csrf_token": token}
        ).status_code)

    def test_auth_guards_queue_and_guest_ui(self):
        anonymous = self.client()
        self.assertEqual(303, anonymous.post("/job/j1/apply", follow_redirects=False).status_code)
        guest = self.client()
        self.login_guest(guest)
        self.assertEqual(403, guest.post("/job/j1/apply").status_code)
        self.assertEqual(403, guest.get("/applications").status_code)
        self.assertNotIn("Start attended application", guest.get("/job/j1?pane=1").text)

    def test_unsafe_role_url_is_refused(self):
        client = self.client()
        self.login_owner(client)
        self.assertEqual(400, client.post("/job/bad/apply").status_code)

    def test_agent_capability_reports_progress_but_cannot_complete(self):
        client = self.client()
        self.login_owner(client)
        queued = html.unescape(client.post("/job/j1/apply").text)
        # The panel hands the agent a fixed command, not a page to open and not
        # a token: the callback stopped being a browser tab on 2026-09-12, and
        # the token left the agent's hands the same day so that one permission
        # grant could cover every status update.
        match = re.search(r"workflow (apply_[a-f0-9]+)\.", queued)
        self.assertIsNotNone(match)
        workflow_id = match.group(1)
        self.assertNotIn("token=", queued)
        token = workflow_token("application-workflow-test-secret", workflow_id)
        callback = f"https://jobfeed.test/application/{workflow_id}/agent?token={token}"

        agent = self.client()  # deliberately not logged in
        response = agent.get(callback)
        self.assertEqual(200, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual("no-referrer", response.headers["referrer-policy"])
        self.assertEqual(200, agent.post(
            f"/application/{workflow_id}/agent/status",
            data={"token": token, "status": "in_progress", "detail": "form open"},
        ).status_code)
        self.assertEqual(403, agent.post(
            f"/application/{workflow_id}/agent/status",
            data={"token": token, "status": "queued"},
        ).status_code)
        self.assertEqual(403, agent.post(
            f"/application/{workflow_id}/agent/status",
            data={"token": token, "status": "completed"},
        ).status_code)
        self.assertEqual(404, agent.post(
            f"/application/{workflow_id}/agent/status",
            data={"token": "wrong", "status": "failed"},
        ).status_code)
        con = sqlite3.connect(self.tmp.name)
        workflow_status = con.execute(
            "SELECT status FROM application_workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()[0]
        crm = con.execute("SELECT status FROM seen_jobs WHERE id='j1'").fetchone()[0]
        con.close()
        self.assertEqual("in_progress", workflow_status)
        self.assertEqual("queued", crm)

    def test_a_status_filter_on_an_acted_on_stage_shows_delisted_roles(self):
        """A firm taking its posting down says nothing about his application:
        "Status: applied" lists every role he applied to (2026-09-17). The
        unfiltered board still hides delisted rows."""
        db = JobDB(self.tmp.name)
        db.set_status("j1", "applied")
        db.conn.execute("UPDATE seen_jobs SET delisted_at='2026-09-10T00:00:00+00:00' WHERE id='j1'")
        db.conn.commit()
        db.conn.close()
        rq = lambda qs: webapp._filters_from_request(
            mock.Mock(query_params=dict(qs)))
        self.assertTrue(rq({})["hide_delisted"])
        self.assertTrue(rq({"status": "new"})["hide_delisted"])
        for stage in ("queued", "applied", "oa", "interview", "offer", "rejected"):
            self.assertFalse(rq({"status": stage})["hide_delisted"], stage)
        self.assertFalse(rq({"show_expired": "1"})["hide_delisted"])
        self.assertFalse(rq({"fav": "1"})["hide_delisted"])
        # Nor does a tag: Tudor's application, tagged area 'other', vanished
        # from the pipeline board's link.
        self.assertTrue(rq({})["hide_other"])
        self.assertFalse(rq({"status": "applied"})["hide_other"])
        self.assertFalse(rq({"status": "applied"})["hide_yoe"])
        self.assertNotIn("hide_associates", rq({"status": "oa"}))

    def test_the_role_pane_shows_the_rejection_reason_and_cool_down(self):
        """A role applied to by hand has no workflow, so before 2026-09-17 its
        rejection and Flow Traders' 12-month cool-down showed nowhere."""
        db = JobDB(self.tmp.name)
        db.record_application_mail({
            "message_key": "flow-1", "received_at": "2026-09-17T11:58:00+00:00",
            "sender": "careers.europe@flowtraders.jobs",
            "subject": "Application Update: Flow Traders", "job_id": "j1",
            "proposed_status": "rejected", "outcome": "applied",
            "evidence": "we will not be able to continue with your candidacy.",
            "match_reason": "model matched",
            "rejection_reason": "your test scores did not meet our global standards.",
            "reason_kind": "assessment", "reapply_after": "2099-09-17",
            "reapply_quote": "a cool-down period of 12-months",
        })
        db.conn.close()
        client = self.client()
        self.login_owner(client)
        pane = html.unescape(client.get("/job/j1?pane=1").text)
        self.assertIn("will not accept a new application for this role until 2099-09-17", pane)
        self.assertIn("a cool-down period of 12-months", pane)
        self.assertIn("your test scores did not meet our global standards.", pane)
        self.assertIn("assessment", pane)

    def test_a_run_with_no_stop_reports_review_ready_straight_from_queued(self):
        """A run that never needs the user has nothing to say until the form is
        filled. Refusing that report cost the BNP run of 2026-09-16 seven
        minutes and two permission prompts, and the start time is kept."""
        client = self.client()
        self.login_owner(client)
        queued = html.unescape(client.post("/job/j1/apply").text)
        workflow_id = re.search(r"workflow (apply_[a-f0-9]+)\.", queued).group(1)
        token = workflow_token("application-workflow-test-secret", workflow_id)
        agent = self.client()
        ready = agent.post(f"/application/{workflow_id}/agent/status",
                           data={"token": token, "status": "review_ready",
                                 "detail": "filled to the final step"})
        self.assertEqual(200, ready.status_code)
        con = sqlite3.connect(self.tmp.name)
        status, started, ready_at = con.execute(
            "SELECT status, started_at, review_ready_at FROM application_workflows "
            "WHERE workflow_id=?", (workflow_id,)).fetchone()
        con.close()
        self.assertEqual("review_ready", status)
        self.assertTrue(started and ready_at)

    def test_a_refused_report_says_which_rule_refused_it(self):
        """`409` on its own sent the BNP run into web/app.py and db.py. The
        reason travels with the response so the CLI can print it."""
        client = self.client()
        self.login_owner(client)
        queued = html.unescape(client.post("/job/j1/apply").text)
        workflow_id = re.search(r"workflow (apply_[a-f0-9]+)\.", queued).group(1)
        token = workflow_token("application-workflow-test-secret", workflow_id)
        agent = self.client()
        agent.post(f"/application/{workflow_id}/agent/status",
                   data={"token": token, "status": "review_ready"})
        refused = agent.post(f"/application/{workflow_id}/agent/status",
                             data={"token": token, "status": "needs_user_action"})
        self.assertEqual(409, refused.status_code)
        self.assertIn("'review_ready' -> 'needs_user_action'",
                      refused.headers["x-jobfeed-message"])

    def test_review_ready_then_owner_completion_is_explicit(self):
        client = self.client()
        self.login_owner(client)
        queued = html.unescape(client.post("/job/j1/apply").text)
        workflow_id = re.search(r"workflow (apply_[a-f0-9]+)\.", queued).group(1)
        token = workflow_token("application-workflow-test-secret", workflow_id)
        agent = self.client()
        agent.post(f"/application/{workflow_id}/agent/status",
                   data={"token": token, "status": "in_progress"})
        ready = agent.post(f"/application/{workflow_id}/agent/status",
                           data={"token": token, "status": "review_ready"})
        self.assertIn("review ready", ready.text)
        owner_panel = client.get("/job/j1?pane=1").text
        self.assertIn("I submitted it", owner_panel)
        done = client.post(f"/application/{workflow_id}/status",
                           data={"status": "completed"})
        self.assertEqual(200, done.status_code)
        self.assertIn("Recorded as applied after your manual submission", done.text)
        queue = client.get("/applications")
        self.assertEqual(200, queue.status_code)
        self.assertIn("Markets Analyst", queue.text)
        self.assertIn("completed", queue.text)


if __name__ == "__main__":
    unittest.main()


class AutopilotQueueWebTests(WorkflowWebTests):
    """Auto apply on several roles fills them one after another: one launch,
    the rest queued, the next only after the first agent is confirmed stopped."""

    def setUp(self):
        super().setUp()
        db = JobDB(self.tmp.name)
        db.conn.execute(
            "INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen, status) "
            "VALUES ('j2', 'OtherCo', 'Trader', 'https://apply.example.test/jobs/two', "
            "'2026-09-12', '2026-09-12', 'new')")
        db.conn.commit()
        db.conn.close()

    def start(self, client, job_id):
        panel = client.post(f"/job/{job_id}/apply")
        workflow_id = re.search(r"/application/(apply_[a-f0-9]+)/launch", panel.text).group(1)
        token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', panel.text).group(1))
        return workflow_id, token, client.post(
            f"/application/{workflow_id}/launch", data={"csrf_token": token})

    def test_two_auto_applies_launch_one_and_queue_the_other(self):
        client = self.client()
        self.login_owner(client)
        with mock.patch.object(webapp, "enqueue_launch") as enqueue:
            first, token, _ = self.start(client, "j1")
            second, _, queued = self.start(client, "j2")
        self.assertEqual(1, enqueue.call_count)
        self.assertEqual(first, enqueue.call_args.args[0]["workflow_id"])
        self.assertIn("Queued behind 1 application", queued.text)
        page = client.get("/applications").text
        self.assertIn("Autopilot queue", page)
        self.assertIn("OtherCo", page)

        # First agent reports review_ready and its parent kills the subagent.
        db = JobDB(self.tmp.name)
        wf = db.get_application_workflow(first)
        run_id = db.active_application_run()["run_id"]
        db.claim_application_launch(wf["launch_request_id"])
        db.finish_application_launch(wf["launch_request_id"], True)
        start = db.active_application_run()["launching_at"]
        db.transition_application_workflow(first, "review_ready", actor="agent")
        db.conn.close()
        from datetime import datetime, timedelta
        t0 = datetime.fromisoformat(start)
        sub = {"conv": "sub-1", "workflow": first, "run_id": run_id,
               "kind": "sub", "started": start,
               "ended": (t0 + timedelta(minutes=5)).isoformat(), "kills": [], "lists": []}
        par = {"conv": "par-1", "workflow": first, "run_id": run_id,
               "kind": "par", "started": start,
               "ended": (t0 + timedelta(minutes=6)).isoformat(), "lists": [],
               "kills": [{"at": (t0 + timedelta(minutes=6)).isoformat(), "ids": ["sub-1"], "ok": True}]}
        with mock.patch.object(webapp, "_agent_runs", return_value=[par, sub]), \
                mock.patch.object(webapp, "enqueue_launch") as enqueue:
            db = JobDB(self.tmp.name)
            for _ in range(4):
                webapp._autopilot_tick(db)
            db.conn.close()
        enqueue.assert_called_once()
        self.assertEqual(second, enqueue.call_args.args[0]["workflow_id"])

    def test_pause_blocks_and_resume_requires_csrf(self):
        client = self.client()
        self.login_owner(client)
        panel = client.post("/job/j1/apply")
        token = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', panel.text).group(1))
        self.assertEqual(403, client.post("/autopilot", data={"action": "pause"}).status_code)
        self.assertEqual(303, client.post("/autopilot", data={"action": "pause", "csrf_token": token},
                                          follow_redirects=False).status_code)
        workflow_id = re.search(r"/application/(apply_[a-f0-9]+)/launch", panel.text).group(1)
        with mock.patch.object(webapp, "enqueue_launch") as enqueue:
            client.post(f"/application/{workflow_id}/launch", data={"csrf_token": token})
        enqueue.assert_not_called()
        with mock.patch.object(webapp, "enqueue_launch") as enqueue:
            client.post("/autopilot", data={"action": "resume", "csrf_token": token})
        enqueue.assert_called_once()

    def test_status_report_must_name_the_active_attempt(self):
        client = self.client()
        self.login_owner(client)
        with mock.patch.object(webapp, "enqueue_launch"):
            workflow_id, _, _ = self.start(client, "j1")
        db = JobDB(self.tmp.name)
        run_id = db.active_application_run()["run_id"]
        db.conn.close()
        token = workflow_token("application-workflow-test-secret", workflow_id)
        agent = self.client()
        url = f"/application/{workflow_id}/agent/status"
        self.assertEqual(409, agent.post(
            url, data={"token": token, "status": "in_progress"}).status_code)
        self.assertEqual(409, agent.post(
            url, data={"token": token, "run_id": "run_oldattempt",
                       "status": "in_progress"}).status_code)
        self.assertEqual(200, agent.post(
            url, data={"token": token, "run_id": run_id,
                       "status": "in_progress"}).status_code)
