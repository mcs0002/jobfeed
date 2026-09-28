"""unplaced_employer_mail decides which unclassified messages are surfaced for
hand-filing against an application. Every rule here was a real misfiling;
the firms below are invented so the fixture records no real application."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobfeed.db import JobDB


class UnplacedEmployerMailTests(unittest.TestCase):
    SINCE = "2026-09-01T00:00:00+00:00"

    def setUp(self):
        self.db = JobDB(":memory:")
        for id_, company, status in [
            ("qxr", "QXR", "applied"),                  # three-letter firm
            ("kes", "Kestrel Capital", "interview"),
            ("ab", "AB Commodities & Trading", "applied"),  # two-letter token
            ("nor", "Norland Bank", "new"),            # no open application
            ("tes", "Tessel Partners", "rejected"),    # closed application
        ]:
            self.db.mark_seen(id_, company=company, title="Analyst",
                              url=f"https://x/{id_}")
            if status != "new":
                self.db.set_status(id_, status)
        self.n = 0

    def _mail(self, sender, subject="Your application", received="2026-09-20T10:00:00+00:00",
              outcome="unclassified", job_id="", resolved=False):
        self.n += 1
        key = f"m{self.n}"
        self.db.record_application_mail({
            "message_key": key, "received_at": received, "sender": sender,
            "subject": subject, "job_id": job_id, "proposed_status": "",
            "outcome": outcome, "evidence": "", "match_reason": "",
        })
        if resolved:
            self.db.conn.execute(
                "UPDATE application_mail_events SET resolved_at='x' WHERE message_key=?",
                (key,))
            self.db.conn.commit()
        return key

    def _found(self, **kw):
        return {r["message_key"]: r
                for r in self.db.unplaced_employer_mail(self.SINCE, **kw)}

    def test_firm_in_sender_is_matched_to_the_application(self):
        key = self._mail("QXR Careers <noreply@qxr.com>")
        row = self._found()[key]
        self.assertEqual((row["sender_match"], row["company"]), ("qxr", "QXR"))

    def test_firm_behind_an_ats_envelope_still_resolves(self):
        # 2026-09-17: the platform name was used as the firm, so a firm's own
        # mail came through with no company and nothing to file it against.
        key = self._mail('"noreply@qxr.com" <qxr@successfactors.eu>')
        row = self._found()[key]
        self.assertEqual(row["company"], "QXR")

    def test_ats_sender_alone_is_surfaced_without_a_firm(self):
        key = self._mail("Workday <noreply@myworkday.com>")
        row = self._found()[key]
        self.assertEqual((row["sender_match"], row["company"]), ("workday", ""))

    def test_token_must_stand_alone(self):
        # "qxr" inside "qxrsignal" is not QXR.
        key = self._mail("QXRSignal Newsletter <news@qxrsignal.com>")
        self.assertNotIn(key, self._found())

    def test_subject_never_matches(self):
        # A job-board advert naming a firm once landed on an application there.
        key = self._mail("LinkedIn <jobs@linkedin.com>", subject="Kestrel is hiring")
        self.assertNotIn(key, self._found())

    def test_only_open_applications_supply_firms(self):
        keys = [self._mail("Norland Bank <careers@norland.com>"),
                self._mail("Tessel <noreply@tessel.com>")]
        found = self._found()
        for key in keys:
            self.assertNotIn(key, found)

    def test_short_first_tokens_are_not_used(self):
        key = self._mail("AB <noreply@ab.com>")
        self.assertNotIn(key, self._found())

    def test_old_placed_classified_and_resolved_mail_is_excluded(self):
        keys = [
            self._mail("QXR <noreply@qxr.com>", received="2026-08-01T10:00:00+00:00"),
            self._mail("QXR <noreply@qxr.com>", job_id="qxr"),
            self._mail("QXR <noreply@qxr.com>", outcome="rejected"),
            self._mail("QXR <noreply@qxr.com>", resolved=True),
        ]
        found = self._found()
        for key in keys:
            self.assertNotIn(key, found)

    def test_newest_first_and_limit_respected(self):
        older = self._mail("QXR <noreply@qxr.com>", received="2026-09-10T10:00:00+00:00")
        newer = self._mail("QXR <noreply@qxr.com>", received="2026-09-25T10:00:00+00:00")
        rows = self.db.unplaced_employer_mail(self.SINCE, limit=1)
        self.assertEqual([r["message_key"] for r in rows], [newer])
        self.assertNotEqual(older, newer)


if __name__ == "__main__":
    unittest.main()
