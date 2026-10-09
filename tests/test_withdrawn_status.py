"""`withdrawn` is his own exit from an application he made: it stays an
application (firm limits, the export guard) and no later mail reopens it."""
import unittest

from jobfeed.db import APPLIED_STATUSES, mail_rank


class WithdrawnStatusTests(unittest.TestCase):
    def test_no_mail_status_outranks_withdrawn(self):
        for status in ("applied", "oa", "interview", "offer", "rejected"):
            self.assertLess(mail_rank(status), mail_rank("withdrawn"), status)

    def test_withdrawn_still_counts_as_an_application(self):
        self.assertIn("withdrawn", APPLIED_STATUSES)


if __name__ == "__main__":
    unittest.main()
