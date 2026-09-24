"""The password filler: it must refuse more readily than it types."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from applications import account as application_account  # noqa: E402
from applications import password as filler  # noqa: E402
from jobfeed.db import JobDB  # noqa: E402

HOST = "gunvor.wd3.myworkdayjobs.com"
URL = f"https://{HOST}/Gunvor_Careers/job/Geneva/Graduate_JR1"


class FakeDevtools:
    """Stands in for Chrome. Records what it was asked to type."""

    def __init__(self, targets, probe, after=None):
        self.targets, self.probe, self.after = targets, probe, after or probe
        self.typed = []
        self.probes = 0

    def call(self, method, params=None, session=None):
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate":
            self.probes += 1
            value = self.probe if self.probes == 1 else self.after
            return {"result": {"value": value}}
        if method == "Input.insertText":
            self.typed.append(params["text"])
            return {}
        raise AssertionError(method)

    def close(self):
        pass


class PasswordFillTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = JobDB(self.path)
        self.db.mark_seen("j1", company="Gunvor Group", title="Graduate", url=URL)
        workflow, _ = self.db.create_application_workflow("j1", URL)
        self.workflow = workflow["workflow_id"]
        self.db.record_application_account(HOST, self.workflow, "svc")
        self.db.conn.close()
        self.env = mock.patch.dict(os.environ, {"JOBS_DB": self.path})
        self.env.start()
        self.email = mock.patch.object(filler, "_profile_email",
                                       return_value="user@example.com")
        self.email.start()
        self.key = mock.patch.object(filler, "_keychain", return_value="pw" * 12)
        self.key.start()

    def tearDown(self):
        self.key.stop(); self.email.stop(); self.env.stop()
        os.unlink(self.path)

    def _run(self, fake):
        with mock.patch.object(filler, "Devtools", return_value=fake):
            return filler.fill(self.workflow)

    def page(self, url=URL):
        return [{"type": "page", "targetId": "t1", "url": url}]

    def test_types_into_a_focused_password_field_on_the_right_host(self):
        fake = FakeDevtools(self.page(), {"focused": True, "isPassword": True, "length": 0},
                            {"focused": True, "isPassword": True, "length": 24})
        self.assertEqual(0, self._run(fake))
        self.assertEqual(["pw" * 12], fake.typed)

    def test_refuses_when_the_focus_is_not_a_password_field(self):
        fake = FakeDevtools(self.page(), {"focused": True, "isPassword": False, "length": 0})
        self.assertEqual(1, self._run(fake))
        self.assertEqual([], fake.typed)

    def test_refuses_when_nothing_is_focused(self):
        fake = FakeDevtools(self.page(), {"focused": False})
        self.assertEqual(1, self._run(fake))
        self.assertEqual([], fake.typed)

    def test_refuses_a_page_on_another_host(self):
        fake = FakeDevtools(self.page("https://evil.test/login"),
                            {"focused": True, "isPassword": True, "length": 0})
        self.assertEqual(1, self._run(fake))
        self.assertEqual([], fake.typed)

    def test_refuses_when_two_pages_of_the_site_are_open(self):
        fake = FakeDevtools(self.page() + self.page(URL + "?x=2"),
                            {"focused": True, "isPassword": True, "length": 0})
        self.assertEqual(1, self._run(fake))
        self.assertEqual([], fake.typed)

    def test_reports_a_short_write_rather_than_claiming_success(self):
        fake = FakeDevtools(self.page(), {"focused": True, "isPassword": True, "length": 0},
                            {"focused": True, "isPassword": True, "length": 3})
        self.assertEqual(1, self._run(fake))

    def test_refuses_a_workflow_with_no_account(self):
        db = JobDB(self.path)
        db.conn.execute("DELETE FROM application_accounts")
        db.conn.commit(); db.conn.close()
        with self.assertRaises(ValueError):
            filler._expected(self.workflow)

    def test_rejects_a_malformed_workflow_id(self):
        for bad in ("", "apply_", "../etc", "apply_ZZZZ"):
            with self.assertRaises(ValueError):
                filler._expected(bad)


_SKILL_MD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "skills", "assisted-apply", "SKILL.md")


# skills/ is private application tooling and is not in the public mirror.
@unittest.skipUnless(os.path.exists(_SKILL_MD), "skills/ not present (public mirror)")
class NotTheWorkingPathTests(unittest.TestCase):
    def test_the_filler_is_documented_as_parked(self):
        """It works, but Chrome prompts for every new DevTools client, so each
        call raced a manual consent and timed out. Kept, not used."""
        skill = open(_SKILL_MD).read()
        self.assertIn("not** the working path", skill)


if __name__ == "__main__":
    unittest.main()
