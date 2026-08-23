import json
import os
import tempfile
import unittest
from unittest.mock import patch

import apply
from db import JobDB


class ApplyLocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "jobs.db")
        self.env = patch.dict(os.environ, {"JOBS_DB": self.db_path})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_local_queue_only_returns_queued_roles(self):
        db = JobDB(self.db_path)
        for jid, status in (("q1", "queued"), ("n1", "new")):
            db.mark_seen(jid, company="Test", title=jid, url=f"https://x/{jid}")
            db.set_status(jid, status)
        self.assertEqual([job["id"] for job in apply.fetch_queue_local()], ["q1"])

    def test_local_mark_applied(self):
        db = JobDB(self.db_path)
        db.mark_seen("q1", company="Test", title="Role", url="https://x/q1")
        apply.mark_applied_local("q1")
        self.assertEqual(JobDB(self.db_path).get_job("q1")["status"], "applied")

    def test_no_letter_profile_does_not_require_narratives(self):
        profile_path = os.path.join(self.tmp.name, "profile.json")
        with open(profile_path, "w") as fp:
            json.dump({
                "full_name": "First Last", "first_name": "First",
                "last_name": "Last", "email": "x@example.com", "phone": "+1",
            }, fp)
        with patch.object(apply.cl, "PROFILE_JSON", profile_path):
            profile, cv_text, samples = apply._load_apply_inputs(False)
        self.assertEqual(profile["full_name"], "First Last")
        self.assertEqual((cv_text, samples), ("", ""))

    def test_german_work_authorization_is_derived_per_destination(self):
        profile = {"nationality": "German", "common_answers": {}}
        cases = (
            ({"loc_country": "Denmark"}, "EU/EEA", "Yes", "No."),
            ({"location": "Lisbon, PT"}, "EU/EEA", "Yes", "No."),
            ({"loc_country": "Switzerland"}, "Switzerland", "Eligible", "No employer"),
            ({"loc_country": "UK"}, "UK", "No", "Yes"),
            ({"location": "New York, US"}, "US", "No", "Yes"),
            ({"loc_country": "Singapore"}, "country-specific", "Not established", "check"),
        )
        for job, market, auth_fragment, sponsorship_fragment in cases:
            with self.subTest(job=job):
                result = apply.work_authorization_answers(profile, job)
                self.assertEqual(result["market"], market)
                self.assertIn(auth_fragment, result["authorized_to_work"])
                self.assertIn(
                    sponsorship_fragment.casefold(),
                    result["require_visa_sponsorship"].casefold(),
                )

    def test_non_german_profile_keeps_explicit_answers(self):
        profile = {
            "nationality": "Canadian",
            "common_answers": {
                "authorized_to_work": "Existing answer",
                "require_visa_sponsorship": "Existing sponsorship answer",
            },
        }
        result = apply.work_authorization_answers(profile, {"loc_country": "France"})
        self.assertEqual(result["market"], "profile")
        self.assertEqual(result["authorized_to_work"], "Existing answer")
        self.assertEqual(
            result["require_visa_sponsorship"], "Existing sponsorship answer"
        )


if __name__ == "__main__":
    unittest.main()
