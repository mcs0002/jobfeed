"""Tests for scrapers._http.assert_complete."""
import unittest

from scrapers._http import assert_complete


class AssertCompleteTests(unittest.TestCase):
    # --- raises when clearly below band ---

    def test_raises_when_count_well_below_band(self):
        with self.assertRaises(RuntimeError) as ctx:
            assert_complete(50, 100, "TestSource")
        self.assertIn("TestSource", str(ctx.exception))
        self.assertIn("50", str(ctx.exception))
        self.assertIn("100", str(ctx.exception))

    def test_raises_when_count_just_below_band(self):
        # 89 of 100 is 89 % < 90 % — should raise
        with self.assertRaises(RuntimeError):
            assert_complete(89, 100, "TestSource")

    def test_raises_when_count_is_zero_but_total_positive(self):
        with self.assertRaises(RuntimeError):
            assert_complete(0, 100, "TestSource")

    # --- passes at or above band ---

    def test_passes_at_exactly_band_boundary(self):
        # 90 of 100 is exactly 90 % — should NOT raise
        assert_complete(90, 100, "TestSource")

    def test_passes_above_band(self):
        assert_complete(95, 100, "TestSource")

    def test_passes_when_count_equals_total(self):
        assert_complete(100, 100, "TestSource")

    def test_passes_when_count_exceeds_total(self):
        # Over-count is fine (dedup artefact)
        assert_complete(105, 100, "TestSource")

    # --- no-op on None / 0 total ---

    def test_noop_on_none_total(self):
        # Should not raise; no total to compare against
        assert_complete(0, None, "TestSource")

    def test_noop_on_zero_total(self):
        assert_complete(0, 0, "TestSource")

    def test_noop_on_zero_total_nonzero_count(self):
        assert_complete(5, 0, "TestSource")

    # --- custom band ---

    def test_custom_band_raises_when_below(self):
        # band=0.95 → need 95 of 100; 94 should raise
        with self.assertRaises(RuntimeError):
            assert_complete(94, 100, "TestSource", band=0.95)

    def test_custom_band_passes_at_threshold(self):
        assert_complete(95, 100, "TestSource", band=0.95)

    # --- error message quality ---

    def test_error_message_contains_source(self):
        with self.assertRaises(RuntimeError) as ctx:
            assert_complete(1, 1000, "Beesite/deutschebank")
        self.assertIn("Beesite/deutschebank", str(ctx.exception))
        self.assertIn("refusing partial result", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
