import unittest
from datetime import datetime, timedelta, timezone

from jobfeed.db import JobDB


class AgeFloorScopeTests(unittest.TestCase):
    def setUp(self):
        self.db = JobDB(":memory:")
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        for id_, company, seen in [
            ("bp_old", "BP", old),
            ("bp_new", "BP", now),
            ("jpm_old", "JPM", old),
        ]:
            self.db.mark_seen(id_, company=company, title=id_, url=f"https://x/{id_}")
            self.db.conn.execute(
                "UPDATE seen_jobs SET last_seen = ? WHERE id = ?", (seen, id_))
        self.db.conn.commit()

    def test_only_companies_limits_floor_to_named_sources(self):
        self.assertEqual(
            self.db.find_age_delistable(21, only_companies={"BP"}), ["bp_old"])

    def test_default_behaviour_unchanged(self):
        self.assertEqual(
            sorted(self.db.find_age_delistable(21, exclude_companies={"BP"})),
            ["jpm_old"])


if __name__ == "__main__":
    unittest.main()
