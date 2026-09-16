"""Guards for the application-limit researcher.

The point of this feature is that a wrong number is worse than no number:
the user decides which of nine UBS locations to spend his one application on
off the back of it. Every test here is aimed at the ways a plausible-looking
figure could get into the DB without a source behind it."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import application_limits as al
from db import JobDB


# The sentence Deutsche Bank actually uses (fetched 2026-09-08). The first
# version of the prefilter scored zero hits on this page because it only looked
# for "maximum of N applications" phrasings.
DB_SENTENCE = ("We'd like you to research carefully exactly what division you "
               "want to work in, and then apply to just one role in one country.")


class PrefilterTests(unittest.TestCase):
    def test_catches_real_deutsche_bank_phrasing(self):
        self.assertTrue(al.sentence_is_capish(DB_SENTENCE))

    def test_catches_faq_question_heading(self):
        self.assertTrue(al.sentence_is_capish("Can I apply to more than one division?"))

    def test_catches_a_cap_with_no_restriction_word(self):
        """Bank of America expresses its cap purely as a quantity of scopes.
        An earlier version required a restriction word (only / maximum / no
        more than) and missed it, even after the page had been rendered."""
        self.assertTrue(al.sentence_is_capish(
            "Yes, you may apply to one office and one division per year."))

    def test_catches_explicit_maximum(self):
        self.assertTrue(al.sentence_is_capish(
            "You may submit a maximum of two applications per recruitment season."))

    def test_ignores_deadlines_and_cohort_sizes(self):
        for s in ("Applications close on 31 October 2026.",
                  "We hire around 160 analysts globally each year.",
                  "The assessment centre lasts one day."):
            self.assertFalse(al.sentence_is_capish(s), s)

    def test_window_contains_the_whole_answer(self):
        """A window triggered by an FAQ *question* must reach the answer below
        it — the number almost never sits in the sentence that fires."""
        text = ("Padding. " * 40 + "\nCan I apply to more than one division?\n"
                + DB_SENTENCE + "\n" + "Trailing. " * 40)
        windows = al.cap_windows(text)
        self.assertEqual(len(windows), 1)
        self.assertIn("just one role in one country", windows[0])

    def test_window_offsets_stay_aligned(self):
        """Regression: an earlier split()+find() walk desynchronised after the
        first unlocatable sentence and returned windows from the wrong part of
        the page."""
        text = ("Alpha. Beta. Gamma. " * 30
                + "You may only submit one application. "
                + "Delta. Epsilon. " * 30)
        windows = al.cap_windows(text)
        self.assertTrue(windows)
        self.assertTrue(any("only submit one application" in w for w in windows))


class VerificationGateTests(unittest.TestCase):
    PAGES = [("https://example.com/faq",
              "Some preamble. " + DB_SENTENCE + " Some trailing text.")]

    def test_accepts_a_verbatim_quote(self):
        rec, reason = al.verify(
            {"has_limit": True, "max_per_cycle": 1, "cycle": "recruitment year",
             "locations_count_separately": True, "shared_across_programmes": None,
             "quote": DB_SENTENCE}, self.PAGES)
        self.assertIsNotNone(rec, reason)
        self.assertEqual(rec["max_per_cycle"], 1)
        self.assertEqual(rec["source_url"], "https://example.com/faq")
        self.assertEqual(rec["locations_count_separately"], 1)
        self.assertIsNone(rec["shared_across_programmes"])

    def test_tolerates_whitespace_and_case_differences(self):
        noisy = DB_SENTENCE.upper().replace(" ", "   ")
        rec, _ = al.verify({"has_limit": True, "max_per_cycle": 1,
                            "quote": noisy}, self.PAGES)
        self.assertIsNotNone(rec)

    def test_rejects_a_quote_that_is_not_on_the_page(self):
        """Gate 2 — the hallucination that matters. A fluent, entirely
        plausible sentence that no fetched page contains."""
        rec, reason = al.verify(
            {"has_limit": True, "max_per_cycle": 2,
             "quote": "Candidates may submit up to two applications per cycle."},
            self.PAGES)
        self.assertIsNone(rec)
        self.assertIn("QUOTE NOT FOUND", reason)

    def test_rejects_a_number_absent_from_its_own_quote(self):
        """Gate 3 — a real sentence with the wrong number stapled to it."""
        rec, reason = al.verify(
            {"has_limit": True, "max_per_cycle": 3, "quote": DB_SENTENCE},
            self.PAGES)
        self.assertIsNone(rec)
        self.assertIn("absent from its own quote", reason)

    def test_rejects_implausible_and_missing_numbers(self):
        for n in (0, -1, 99, None, "two"):
            rec, _ = al.verify({"has_limit": True, "max_per_cycle": n,
                                "quote": DB_SENTENCE}, self.PAGES)
            self.assertIsNone(rec, n)

    def test_no_limit_is_a_valid_answer(self):
        rec, reason = al.verify({"has_limit": False}, self.PAGES)
        self.assertIsNone(rec)
        self.assertEqual(reason, "no limit stated")


class StrengthTests(unittest.TestCase):
    """A recommendation and a rule are different constraints. J.P. Morgan says
    "we recommend you focus on no more than three summer internship program
    applications"; Macquarie says "we only accept one application per person".
    Presenting the first as a rule would cost applications he may legitimately
    make."""
    PAGES = [("https://x/faq", "We recommend you focus on no more than "
                               "three summer internship program applications.")]

    def test_advisory_is_preserved(self):
        rec, _ = al.verify(
            {"has_limit": True, "max_per_cycle": 3, "strength": "advisory",
             "quote": "We recommend you focus on no more than three summer "
                      "internship program applications."}, self.PAGES)
        self.assertEqual(rec["strength"], "advisory")

    def test_unrecognised_strength_becomes_blank_not_hard(self):
        rec, _ = al.verify(
            {"has_limit": True, "max_per_cycle": 3, "strength": "probably firm",
             "quote": "We recommend you focus on no more than three summer "
                      "internship program applications."}, self.PAGES)
        self.assertEqual(rec["strength"], "")


class RegionVaryingCapTests(unittest.TestCase):
    """UBS sets one allowance for the UK, another for the rest of EMEA, and a
    third for Switzerland/US/APAC. the user starred that one programme in nine
    locations spanning all three, so a single integer is not merely imprecise
    here — it is wrong for most of his list."""
    TEXT = ("It actually depends on the region. For the UK and EMEA, you may "
            "only apply for one program within the UK and one in the rest of "
            "EMEA (excluding Switzerland) during the same academic year. For "
            "roles based in Switzerland, the US or in APAC, we accept a "
            "maximum of three applications across all our business divisions "
            "and regional offices during the same academic year.")
    PAGES = [("https://ubs.test/faq", TEXT)]

    def test_flag_survives_and_takes_the_smallest_allowance(self):
        rec, reason = al.verify(
            {"has_limit": True, "max_per_cycle": 1, "strength": "hard",
             "varies_by_region": True,
             "quote": "For the UK and EMEA, you may only apply for one program "
                      "within the UK and one in the rest of EMEA (excluding "
                      "Switzerland) during the same academic year."},
            self.PAGES)
        self.assertIsNotNone(rec, reason)
        self.assertEqual(rec["varies_by_region"], 1)
        self.assertEqual(rec["max_per_cycle"], 1)

    def test_absent_flag_stays_none_rather_than_false(self):
        """Unstated is not the same as "does not vary" — the UI treats None as
        "nothing said about regions", which is the truthful reading."""
        rec, _ = al.verify(
            {"has_limit": True, "max_per_cycle": 1,
             "quote": "For the UK and EMEA, you may only apply for one program "
                      "within the UK and one in the rest of EMEA (excluding "
                      "Switzerland) during the same academic year."},
            self.PAGES)
        self.assertIsNone(rec["varies_by_region"])

    def test_store_round_trips_the_flag(self):
        db = JobDB(":memory:")
        db.set_company_limit("UBS", max_per_cycle=1, varies_by_region=1,
                             confidence="stated", quote="q", source_url="u")
        self.assertEqual(db.get_company_limit("UBS")["varies_by_region"], 1)


class RenderTriggerTests(unittest.TestCase):
    """The browser fallback must fire on a refusal or a thin page, never on a
    404. Conventional-path probing generates mostly-404 candidate URLs by
    design, and rendering each one dropped a 300-firm sweep to ~1.6 firms a
    minute."""

    def _fetch_with(self, status, body="<html><body>%s</body></html>" % ("x " * 500)):
        calls = {"rendered": 0}

        class FakeResp:
            status_code = status
            headers = {"content-type": "text/html"}
            text = body

        real_get, real_render = al.http_get, al.fetch_rendered
        al.http_get = lambda url: FakeResp()

        def fake_render(url):
            calls["rendered"] += 1
            return "rendered text " * 100, None

        al.fetch_rendered = fake_render
        try:
            text, err = al.fetch("https://x.test/faq")
        finally:
            al.http_get, al.fetch_rendered = real_get, real_render
        return calls["rendered"], text, err

    def test_404_does_not_render(self):
        rendered, text, err = self._fetch_with(404)
        self.assertEqual(rendered, 0)
        self.assertEqual(text, "")
        self.assertIn("404", err)

    def test_403_renders(self):
        rendered, text, _ = self._fetch_with(403)
        self.assertEqual(rendered, 1)
        self.assertTrue(text)

    def test_thin_200_renders(self):
        rendered, text, _ = self._fetch_with(200, "<html><body>hi</body></html>")
        self.assertEqual(rendered, 1)
        self.assertTrue(text)

    def test_healthy_200_does_not_render(self):
        rendered, text, _ = self._fetch_with(200)
        self.assertEqual(rendered, 0)
        self.assertTrue(text)


class ProbePathTests(unittest.TestCase):
    def test_probes_landing_directory_parent_and_root(self):
        paths = al.conventional_paths("https://www.goldmansachs.com/careers/students")
        self.assertIn("https://www.goldmansachs.com/careers/students/faq", paths)
        self.assertIn("https://www.goldmansachs.com/careers/faq", paths)
        self.assertIn("https://www.goldmansachs.com/faq", paths)

    def test_probes_are_unique(self):
        paths = al.conventional_paths("https://example.com/a/b")
        self.assertEqual(len(paths), len(set(paths)))


class CompanyLimitStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = JobDB(":memory:")

    def test_number_survives_only_with_a_source_grade(self):
        self.db.set_company_limit("Stated Co", max_per_cycle=1,
                                  confidence="stated", quote="q",
                                  source_url="u", updated_by="research")
        self.db.set_company_limit("Manual Co", max_per_cycle=2,
                                  confidence="manual", updated_by="manual")
        self.assertEqual(self.db.get_company_limit("Stated Co")["max_per_cycle"], 1)
        self.assertEqual(self.db.get_company_limit("Manual Co")["max_per_cycle"], 2)

    def test_unknown_and_fetch_failed_cannot_carry_a_number(self):
        """Defence in depth: even if a caller passes a number alongside a
        non-source confidence, it must not reach the database."""
        for conf in ("unknown", "fetch_failed"):
            self.db.set_company_limit(f"{conf} Co", max_per_cycle=3,
                                      confidence=conf)
            self.assertIsNone(
                self.db.get_company_limit(f"{conf} Co")["max_per_cycle"], conf)

    def test_rejects_an_unknown_confidence_grade(self):
        with self.assertRaises(ValueError):
            self.db.set_company_limit("X", max_per_cycle=1, confidence="probably")

    def test_upsert_replaces_rather_than_duplicates(self):
        self.db.set_company_limit("Co", max_per_cycle=None, confidence="unknown")
        self.db.set_company_limit("Co", max_per_cycle=1, confidence="manual")
        self.assertEqual(len(self.db.all_company_limits()), 1)
        self.assertEqual(self.db.get_company_limit("Co")["max_per_cycle"], 1)

    def test_applied_counts_use_status_not_the_applied_at_stamp(self):
        """applied_at survives a status change back to 'new', and rows like
        that exist in the live DB — counting it would overstate the quota
        consumed and could stop him applying somewhere he still can."""
        for i, status in enumerate(
                ["applied", "rejected", "interview", "queued", "ignored", "new"]):
            self.db.mark_seen(f"j{i}", company="Acme", title="t", url=f"u{i}")
            self.db.set_status(f"j{i}", status)
        # A row stamped applied_at but moved back to 'new' must not count.
        self.db.mark_seen("j9", company="Acme", title="t", url="u9")
        self.db.set_status("j9", "applied")
        self.db.set_status("j9", "new")
        self.assertEqual(self.db.applied_counts_by_company().get("Acme"), 3)


if __name__ == "__main__":
    unittest.main()
