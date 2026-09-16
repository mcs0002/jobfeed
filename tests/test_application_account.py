"""Narrow recruiting-account and verification broker contracts."""
import json
from pathlib import Path
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("WEB_PASSWORD", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from application_account import _domain_ok, _earlier_account_mail, credentials, verification
from db import JobDB


@mock.patch("application_account._earlier_account_mail", new=lambda host, before: None)
class AccountBrokerTests(unittest.TestCase):
    def test_an_ats_sender_may_link_to_an_ats_host(self):
        """Citi advertises at jobs.citi.com and authenticates on Workday. Its
        reset mail came from otp.workday.com pointing at
        citi.wd5.myworkdayjobs.com, and three valid links were refused."""
        from application_account import _domain_ok
        self.assertTrue(_domain_ok("jobs.citi.com", "otp.workday.com",
                                   "citi.wd5.myworkdayjobs.com"))
        # The widening is bounded by who sent it, not by where it points.
        self.assertFalse(_domain_ok("jobs.citi.com", "phisher.test",
                                    "citi.wd5.myworkdayjobs.com"))
        self.assertFalse(_domain_ok("jobs.citi.com", "otp.workday.com", "evil.test"))
        # And the ordinary same-firm case still holds.
        self.assertTrue(_domain_ok("jobs.bunge.com", "bunge.com", "jobs.bunge.com"))

    def test_fixed_request_file_replaces_the_varying_workflow_flag(self):
        import application_account as broker
        path = broker.FIXED_REQUEST
        self.assertEqual(
            "/private/tmp/the user-application-account-request.json", str(path)
        )
        with mock.patch.object(broker, "FIXED_REQUEST", path):
            for bad in ({"workflow_id": "apply_1", "action": "rm"},
                        {"workflow_id": "apply_1"},
                        {"workflow_id": "apply_1", "action": "credentials", "x": 1}):
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                    json.dump(bad, fh)
                with mock.patch.object(broker, "FIXED_REQUEST", Path(fh.name)):
                    with self.assertRaises(ValueError):
                        broker._fixed_request()
                os.unlink(fh.name)
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                json.dump({"workflow_id": "apply_1", "action": "credentials"}, fh)
            with mock.patch.object(broker, "FIXED_REQUEST", Path(fh.name)):
                self.assertEqual(("apply_1", "credentials"), broker._fixed_request())
            os.unlink(fh.name)

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = JobDB(self.tmp.name)
        self.db.conn.execute(
            "INSERT INTO seen_jobs (id,company,title,url,first_seen,status) "
            "VALUES ('j1','Example Bank','Analyst','https://example.workday.com/tenant/job/1','2026-09-12','new')"
        )
        self.db.conn.commit()
        self.db.create_application_workflow(
            "j1", "https://example.workday.com/tenant/job/1", workflow_id="apply_12345678"
        )
        self.db.conn.close()
        self.env = mock.patch.dict(os.environ, {"JOBS_DB": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop(); os.unlink(self.tmp.name)

    @staticmethod
    def _stored(run, service):
        calls = [c.args[0] for c in run.call_args_list
                 if "add-generic-password" in c.args[0] and c.args[0][c.args[0].index("-s") + 1] == service]
        assert len(calls) == 1, calls
        return calls[0]

    @mock.patch("application_account._profile_email", return_value="applicant@example.test")
    @mock.patch("application_account.subprocess.run")
    def test_security_answers_are_generated_once_and_survive_a_reset(self, run, _email):
        """UBS, 2026-09-15: registration demanded three security questions and
        the run stopped there to ask."""
        store = {}
        service = "jobfeed-application:example.workday.com/tenant"

        def fake_run(args, **kwargs):
            if "add-generic-password" in args:
                store[args[args.index("-s") + 1]] = args[args.index("-w") + 1]
            return mock.Mock(returncode=0, stdout="")

        run.side_effect = fake_run
        with mock.patch("application_account._keychain",
                        side_effect=lambda svc, account: store.get(svc)):
            first = credentials("apply_12345678")
            answers = first["security_answers"]
            self.assertEqual(3, len(set(answers)))
            self.assertTrue(all(a.isalnum() and a[0].isalpha() for a in answers))
            self.assertEqual(json.dumps(answers), store[service + ":security-answers"])
            reset = credentials("apply_12345678", rotate=True)
            self.assertNotEqual(first["password"], reset["password"])
            self.assertEqual(answers, reset["security_answers"])

    def test_mail_from_the_firm_before_the_workflow_marks_an_earlier_account(self):
        """UBS's account came from a November 2025 application by hand, so the
        first attended run's fresh password could never sign in."""
        import application_account as broker
        mails = [
            {"from": '"UBS" <donotreply@trm.brassring.com>', "date": "2025-11-02T07:31:30+00:00",
             "subject": "Your candidate reference number - UBS."},
            {"from": '"UBS Careers" <donotreply@ubs.com>', "date": "2025-11-05T08:53:11+00:00",
             "subject": "Your application for 2026 Off-Cycle Internship"},
            {"from": '"UBS Careers" <donotreply@ubs.com>', "date": "2026-09-15T12:00:00+00:00",
             "subject": "Thank you for applying"},
            {"from": "Newsletter <x@subs.com>", "date": "2024-01-01T00:00:00+00:00", "subject": "hi"},
        ]
        with mock.patch.object(broker.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=json.dumps(mails))) as run:
            found = _earlier_account_mail("jobs.ubs.com", "2026-09-15T09:15:00+00:00")
            self.assertEqual("ubs", run.call_args.args[0][run.call_args.args[0].index("--from") + 1])
        self.assertEqual({"date": "2025-11-05",
                          "subject": "Your application for 2026 Off-Cycle Internship"}, found)
        # A shared ATS domain says nothing about the firm, and the mailbox is
        # never asked.
        with mock.patch.object(broker.subprocess, "run") as run:
            self.assertIsNone(_earlier_account_mail("bank.wd3.myworkdayjobs.com", None))
            run.assert_not_called()
        with mock.patch.object(broker.subprocess, "run", side_effect=OSError):
            self.assertIsNone(_earlier_account_mail("jobs.ubs.com", None))

    def test_a_brand_tld_still_finds_the_firms_ordinary_mail(self):
        """BNP Paribas recruits from group.bnpparibas and writes from
        bnpparibas.com, so the careers host and the sender share no registrable
        domain at all (2026-09-16)."""
        import application_account as broker
        mails = [
            {"from": "BNP Paribas <noreply@mail.bnpparibas.com>",
             "date": "2026-01-08T09:00:00+00:00", "subject": "Your internship offer"},
            {"from": "Other <news@notbnpparibas.example>",
             "date": "2025-01-01T00:00:00+00:00", "subject": "no"},
        ]
        with mock.patch.object(broker.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=json.dumps(mails))) as run:
            found = _earlier_account_mail("group.bnpparibas", "2026-09-16T12:00:00+00:00")
            args = run.call_args.args[0]
            self.assertEqual("bnpparibas", args[args.index("--from") + 1])
        self.assertEqual({"date": "2026-01-08", "subject": "Your internship offer"}, found)

    @mock.patch("application_account._profile_email", return_value="applicant@example.test")
    @mock.patch("application_account._keychain", return_value="StoredPassword1!")
    @mock.patch("application_account.subprocess.run")
    def test_a_stored_password_is_not_a_confirmed_account(self, run, _keychain, _email):
        run.return_value.returncode = 0
        result = credentials("apply_12345678")
        self.assertFalse(result["created"])
        self.assertFalse(result["confirmed"])
        from application_account import mark_verified
        mark_verified("apply_12345678")
        self.assertTrue(credentials("apply_12345678")["confirmed"])

    @mock.patch("application_account._profile_email", return_value="applicant@example.test")
    @mock.patch("application_account._keychain", return_value=None)
    @mock.patch("application_account.subprocess.run")
    def test_unique_password_goes_to_keychain_not_database(self, run, _keychain, _email):
        run.return_value.returncode = 0
        result = credentials("apply_12345678")
        self.assertTrue(result["created"])
        # The password is read off the keychain call, because the function no
        # longer hands it back: it used to, and that put a live credential into
        # the Antigravity trajectory. application_password.py types it instead.
        args = self._stored(run, "jobfeed-application:example.workday.com/tenant")
        self.assertEqual("/usr/bin/security", args[0])
        password = args[args.index("-w") + 1]
        self.assertEqual(16, len(password))  # fits SuccessFactors' 15-18
        # The password is returned on purpose: see credentials(). What must
        # never happen is it reaching the database.
        self.assertEqual(password, result["password"])
        con = JobDB(self.tmp.name)
        row = con.conn.execute("SELECT * FROM application_accounts").fetchone()
        columns = [d[0] for d in con.conn.execute("SELECT * FROM application_accounts").description]
        con.conn.close()
        self.assertNotIn("password", columns)
        self.assertNotIn(password, [str(v) for v in row])

    @mock.patch("application_account._profile_email", return_value="applicant@example.test")
    @mock.patch("application_account._keychain", return_value="OldPassword1!")
    @mock.patch("application_account.subprocess.run")
    def test_authorized_reset_rotates_only_current_site_keychain_item(self, run, _keychain, _email):
        run.return_value.returncode = 0
        result = credentials("apply_12345678", rotate=True)
        self.assertTrue(result["rotated"])
        args = self._stored(run, "jobfeed-application:example.workday.com/tenant")
        rotated = args[args.index("-w") + 1]
        self.assertNotEqual("OldPassword1!", rotated)
        self.assertEqual(16, len(rotated))
        self.assertEqual(rotated, result["password"])

    @mock.patch("application_account._profile_email", return_value="applicant@example.test")
    @mock.patch("application_account.subprocess.run")
    def test_verification_returns_only_correlated_recent_code(self, run, _email):
        db = JobDB(self.tmp.name)
        db.record_application_account("example.workday.com/tenant", "apply_12345678",
                                      "jobfeed-application:example.workday.com/tenant")
        stamp = datetime.now(timezone.utc).isoformat()
        db.conn.execute("UPDATE application_accounts SET prepared_at=?", (stamp,))
        db.conn.commit(); db.conn.close()
        message = [{"from": "Example Bank <verify@workday.com>",
                    "to": "applicant@example.test", "date": stamp,
                    "subject": "Verify your Example Bank account",
                    "body": "Your verification code is 482915."}]
        run.return_value = mock.Mock(returncode=0, stdout=json.dumps(message))
        self.assertEqual({"status": "code", "value": "482915"},
                         verification("apply_12345678"))

    @mock.patch("application_account._profile_email", return_value="applicant@example.test")
    @mock.patch("application_account.subprocess.run")
    def test_code_sent_just_before_the_account_was_prepared_is_found(self, run, _email):
        """Goldman Sachs, 2026-09-13: the code arrived 19 s before `credentials`
        recorded the account and ten polls skipped it."""
        db = JobDB(self.tmp.name)
        db.record_application_account("example.workday.com/tenant", "apply_12345678",
                                      "jobfeed-application:example.workday.com/tenant")
        now = datetime.now(timezone.utc)
        db.conn.execute("UPDATE application_accounts SET prepared_at=?", (now.isoformat(),))
        db.conn.execute("UPDATE application_workflows SET created_at=?",
                        ((now - timedelta(minutes=2)).isoformat(),))
        db.conn.commit(); db.conn.close()

        def mail(minutes, code):
            return {"from": "Example Bank Recruiting <x@workday.com>",
                    "to": "applicant@example.test",
                    "date": (now - timedelta(minutes=minutes)).isoformat(),
                    "subject": "Your verification code", "body": f"Your code is {code}."}
        # 5 min old predates the workflow; 1 min old is the real one; 0.3 is a resend.
        run.return_value = mock.Mock(returncode=0, stdout=json.dumps(
            [mail(5, "111111"), mail(1, "222222"), mail(0.3, "333333")]))
        self.assertEqual("333333", verification("apply_12345678")["value"])
        run.return_value = mock.Mock(returncode=0, stdout=json.dumps([mail(5, "111111")]))
        self.assertEqual({"status": "pending"}, verification("apply_12345678"))

    def test_confirmation_link_must_match_application_sender_or_ats_domain(self):
        self.assertTrue(_domain_ok("jobs.example.test", "mail.example.test", "verify.example.test"))
        self.assertFalse(_domain_ok("jobs.example.test", "mail.example.test", "evil.test"))


if __name__ == "__main__":
    unittest.main()
