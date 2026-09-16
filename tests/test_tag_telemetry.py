import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from tag_telemetry import aggregate, price


class TagTelemetryTests(unittest.TestCase):
    def test_aggregate_run_health(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {"ts": now, "run_id": "r1", "provider": "api", "model": "deepseek-v4-pro",
             "rubric_version": "abc", "jobs_total": 10, "jobs_tagged": 9,
             "tokens_in": 1000, "tokens_cached": 500, "tokens_out": 100,
             "latency_s": 2, "batches_failed": 1, "api_fallback": True},
            {"ts": now, "run_id": "r1", "provider": "api", "model": "deepseek-v4-pro",
             "rubric_version": "abc", "jobs_total": 10, "jobs_tagged": 10,
             "tokens_in": 1000, "tokens_cached": 1000, "tokens_out": 100,
             "latency_s": 3},
        ]
        with open(path, "w") as fp:
            for row in rows:
                fp.write(json.dumps(row) + "\n")
            fp.write("not-json\n")
        out = aggregate(path=path)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["coverage"], 95.0)
        self.assertEqual(out[0]["cache_rate"], 75.0)
        self.assertEqual(out[0]["rubric"], "abc")
        self.assertEqual(out[0]["flags"], ["api_fallback"])
        self.assertTrue(out[0]["priced"])

    def test_unknown_model_is_unpriced(self):
        self.assertIsNone(price({"model": "unknown"}, datetime.now(timezone.utc)))


if __name__ == "__main__":
    unittest.main()
