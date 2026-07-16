"""Unit tests for tag.parse_response / coercion / tag_jobs. No CLI calls."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tag
from tag import parse_response, _coerce

# Field order (13 fields, 12 pipes):
# INDEX|AREA|DESK|SENIORITY|TYPE|CITY|COUNTRY|REGION|WORKMODE|LANG_REQ|MIN_YOE|EDUCATION|START_DATE

# A fully-tagged markets row the fake batch can reuse.
_MARKETS_TAGS = {
    "area": "markets", "desk": "trading", "seniority": "graduate",
    "job_type": "job", "loc_city": "London",
    "loc_country": "United Kingdom", "loc_region": "Europe",
    "work_mode": "onsite",
}


class ParseResponseTests(unittest.TestCase):
    def test_markets_with_desk(self):
        out = parse_response(
            "0|markets|trading|graduate|graduate-programme|London|United Kingdom|Europe|onsite|-|0|bachelor|2026-09\n",
            expected_count=1,
        )
        self.assertEqual(out[0], {
            "area": "markets", "desk": "trading", "seniority": "graduate",
            "job_type": "graduate-programme", "loc_city": "London",
            "loc_country": "United Kingdom", "loc_region": "Europe",
            "work_mode": "onsite",
            "lang_req": "", "education": "bachelor", "start_date": "2026-09",
            "min_yoe": 0,
        })

    def test_ibd_desk_blank(self):
        out = parse_response(
            "0|ibd|-|analyst|job|Frankfurt|Germany|Europe|onsite|-|0|-|-\n",
            expected_count=1)
        self.assertEqual(out[0]["area"], "ibd")
        self.assertEqual(out[0]["desk"], "")

    def test_other_no_desk(self):
        out = parse_response(
            "0|other|-|analyst|job|Essen|Germany|Europe|onsite|-|0|-|-\n",
            expected_count=1)
        self.assertEqual(out[0]["area"], "other")
        self.assertEqual(out[0]["desk"], "")

    def test_desk_dropped_when_not_markets(self):
        out = parse_response(
            "0|quant|trading|graduate|job|Hong Kong|Hong Kong|APAC|hybrid|-|3|phd|asap\n",
            expected_count=1)
        self.assertEqual(out[0]["area"], "quant")
        self.assertEqual(out[0]["desk"], "")  # desk only within markets

    def test_unknown_area_coerced_to_other(self):
        out = parse_response(
            "0|wizardry|-|graduate|job|London|UK|Europe|onsite|-|0|-|-\n",
            expected_count=1)
        self.assertEqual(out[0]["area"], "other")

    def test_unknown_region_blanked(self):
        out = parse_response(
            "0|markets|sales|graduate|job|Atlantis|Nowhere|Mars|onsite|-|0|-|-\n",
            expected_count=1)
        self.assertEqual(out[0]["loc_region"], "")

    def test_lowercase_region_snaps_to_vocab(self):
        # Region is matched case-insensitively (every other field is lowercased
        # before coercion, so a model reply of "europe"/"apac" must still snap).
        out = parse_response(
            "0|markets|sales|graduate|job|London|United Kingdom|europe|onsite|-|0|-|-\n"
            "1|markets|sales|graduate|job|Singapore|Singapore|apac|onsite|-|0|-|-\n",
            expected_count=2)
        self.assertEqual(out[0]["loc_region"], "Europe")
        self.assertEqual(out[1]["loc_region"], "APAC")

    def test_wrong_field_count_skipped(self):
        out = parse_response("0|markets|trading|graduate\n", expected_count=1)
        self.assertEqual(out, {})

    def test_old_eight_field_line_rejected(self):
        # The pre-upgrade 8-field format must NOT parse under the 12-field
        # schema (a partial parse would write garbage into the new columns).
        out = parse_response(
            "0|markets|trading|graduate|job|London|UK|Europe|onsite\n",
            expected_count=1)
        self.assertEqual(out, {})

    def test_out_of_range_index_dropped(self):
        out = parse_response(
            "5|markets|trading|graduate|job|London|UK|Europe|onsite|-|0|-|-\n",
            expected_count=2)
        self.assertEqual(out, {})

    def test_preamble_ignored(self):
        out = parse_response(
            "Here you go:\n"
            "0|ibd|-|analyst|job|Frankfurt|Germany|Europe|onsite|-|0|-|-\n",
            expected_count=1)
        self.assertEqual(out[0]["area"], "ibd")


class DescFacetParseTests(unittest.TestCase):
    """Parsing + coercion of the four description-derived fields."""

    def _one(self, line: str) -> dict:
        return parse_response(line + "\n", expected_count=1)[0]

    def test_lang_req_multi_and_english_dropped(self):
        # "en" is the baseline and never emitted; off-vocab codes are dropped;
        # order + dedupe preserved.
        r = self._one("0|markets|-|analyst|job|Geneva|Switzerland|Europe|onsite|en,fr,de,fr,xx|0|-|-")
        self.assertEqual(r["lang_req"], "fr,de")

    def test_lang_req_empty(self):
        r = self._one("0|markets|-|analyst|job|London|UK|Europe|onsite|-|0|-|-")
        self.assertEqual(r["lang_req"], "")

    def test_education_off_vocab_blanked(self):
        r = self._one("0|ibd|-|analyst|job|London|UK|Europe|onsite|-|0|doctorate|-")
        self.assertEqual(r["education"], "")  # 'doctorate' not in vocab
        r2 = self._one("0|ibd|-|analyst|job|London|UK|Europe|onsite|-|0|PhD|-")
        self.assertEqual(r2["education"], "phd")  # case-folded to vocab

    def test_min_yoe_clamped_and_nonnumeric(self):
        self.assertEqual(
            self._one("0|ibd|-|analyst|job|London|UK|Europe|onsite|-|99|-|-")["min_yoe"], 30)
        self.assertEqual(
            self._one("0|ibd|-|analyst|job|London|UK|Europe|onsite|-|many|-|-")["min_yoe"], 0)
        self.assertEqual(
            self._one("0|ibd|-|analyst|job|London|UK|Europe|onsite|-|3|-|-")["min_yoe"], 3)

    def test_start_date_validation(self):
        good = {
            "asap": "asap", "2026": "2026", "2026-09": "2026-09",
            "2026-01": "2026-01",
        }
        for raw, exp in good.items():
            r = self._one(f"0|ibd|-|analyst|job|London|UK|Europe|onsite|-|0|-|{raw}")
            self.assertEqual(r["start_date"], exp, raw)
        for bad in ("2026-13", "2026-00", "soon", "Q1 2026", "09-2026"):
            r = self._one(f"0|ibd|-|analyst|job|London|UK|Europe|onsite|-|0|-|{bad}")
            self.assertEqual(r["start_date"], "", bad)


class DescExcerptTests(unittest.TestCase):
    """The spliced excerpt builder (_desc_excerpt / _requirements_section)."""

    def test_deep_profile_section_is_spliced(self):
        # A "Your profile" section sits ~2k chars into the body, past a flat
        # first-N window — the builder must still splice it in so the
        # description-derived facets have signal to read.
        lead = "About the role. " * 120  # ~1,900 chars of lead prose
        job = {
            "title": "Credit Analyst",
            "description": (
                lead
                + "\n\nYour profile:\n"
                + "- Fluent German required\n"
                + "- 3-5 years of experience\n"
                + "- Master's degree required\n"
            ),
        }
        ex = tag._desc_excerpt(job)
        self.assertIn("[REQUIREMENTS]", ex)
        self.assertIn("Fluent German required", ex)
        self.assertIn("Master's degree required", ex)
        # And the lead is present too (opening slice).
        self.assertIn("About the role.", ex)

    def test_no_heading_falls_back_to_flat_window(self):
        job = {"title": "X", "description": "Just a flat blurb with no headings. " * 5}
        ex = tag._desc_excerpt(job)
        self.assertNotIn("[REQUIREMENTS]", ex)
        self.assertTrue(ex.startswith("Just a flat blurb"))

    def test_empty_description(self):
        self.assertEqual(tag._desc_excerpt({"description": ""}), "")
        self.assertEqual(tag._desc_excerpt({}), "")

    def test_total_cap_enforced(self):
        job = {
            "title": "X",
            "description": ("Lead. " * 400) + "\n\nRequirements:\n" + ("req line. " * 400),
        }
        ex = tag._desc_excerpt(job)
        self.assertLessEqual(len(ex), tag.EXCERPT_TOTAL_CAP)


class CoerceTests(unittest.TestCase):
    def test_valid_markets_row(self):
        self.assertEqual(
            _coerce("markets", "sales", "analyst", "job", "Europe", "hybrid"),
            ("markets", "sales", "analyst", "job", "Europe", "hybrid"),
        )

    def test_invalid_values_blanked(self):
        (a, d, s, jt, r, wm) = _coerce(
            "research", "trading", "boss", "x", "Europe", "telepathic")
        self.assertEqual(a, "research")
        self.assertEqual(d, "")          # desk only within markets
        self.assertEqual(s, "")          # invalid seniority blanked
        self.assertEqual(jt, "job")      # invalid type -> job
        self.assertEqual(wm, "")         # invalid work mode blanked

    def test_capital_markets_is_valid_area(self):
        a, *_ = _coerce("capital-markets", "", "analyst", "job",
                        "Europe", "onsite")
        self.assertEqual(a, "capital-markets")

    def test_middle_office_and_consulting_are_valid_areas(self):
        for area in ("middle-office", "consulting", "accounting", "wealth"):
            a, d, *_ = _coerce(area, "trading", "analyst", "job",
                               "Europe", "onsite")
            self.assertEqual(a, area)        # not coerced to 'other'
            self.assertEqual(d, "")          # desk only valid within markets


class TagJobsTests(unittest.TestCase):
    def test_tags_applied_in_place(self):
        jobs = [{"title": "FX Trader", "company": "GS", "location": "London",
                 "category": "Global Investment Banks"}]

        def fake_batch(batch, bin_path, health=None):
            for j in batch:
                j.update(_MARKETS_TAGS)

        with patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "_tag_batch", side_effect=fake_batch):
            tag.tag_jobs(jobs)
        self.assertEqual(jobs[0]["area"], "markets")
        self.assertEqual(jobs[0]["desk"], "trading")
        self.assertEqual(tag.LAST_RUN_HEALTH["jobs_tagged"], 1)

    def test_no_cli_leaves_blank_tags(self):
        jobs = [{"title": "FX Trader", "company": "GS", "location": "London"}]
        with patch.object(tag, "_claude_bin", return_value=None):
            tag.tag_jobs(jobs)
        self.assertEqual(jobs[0]["area"], "")
        self.assertEqual(jobs[0]["desk"], "")
        self.assertEqual(jobs[0]["job_type"], "job")
        self.assertEqual(tag.LAST_RUN_HEALTH["jobs_tagged"], 0)

    def test_payload_includes_sector_and_description(self):
        jobs = [{"title": "Sales Manager", "company": "Centrica",
                 "location": "Windsor",
                 "category": "Energy Utilities w/ Trading",
                 "description": "<p>Manage B2B power supply contracts.</p>"}]
        payload = tag._build_payload(jobs)
        self.assertIn("Sector: Energy Utilities w/ Trading", payload)
        self.assertIn("Manage B2B power supply contracts.", payload)
        self.assertNotIn("<p>", payload)  # HTML stripped from the snippet

    def test_empty_list_safe(self):
        self.assertEqual(tag.tag_jobs([]), [])

    def test_internship_type_forced_from_title(self):
        # Even if the tagger calls it a plain job, an internship-shaped title
        # must end up job_type=internship so the web app hides it by default.
        jobs = [{"title": "Sales & Trading Summer Internship 2026",
                 "company": "GS", "location": "London"}]

        def fake_batch(batch, bin_path, health=None):
            for j in batch:
                j.update(_MARKETS_TAGS)

        with patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "_tag_batch", side_effect=fake_batch):
            tag.tag_jobs(jobs)
        self.assertEqual(jobs[0]["job_type"], "internship")
        self.assertEqual(jobs[0]["seniority"], "intern")
        self.assertEqual(jobs[0]["area"], "markets")  # area is untouched

    def test_internship_override_skipped_when_no_cli(self):
        # Title-level enforcement also runs on the no-CLI blanked path.
        jobs = [{"title": "Praktikum Treasury", "company": "DB", "location": "Frankfurt"}]
        with patch.object(tag, "_claude_bin", return_value=None):
            tag.tag_jobs(jobs)
        self.assertEqual(jobs[0]["job_type"], "internship")

    def test_non_internship_unaffected(self):
        jobs = [{"title": "FX Trader", "company": "GS", "location": "London"}]
        with patch.object(tag, "_claude_bin", return_value=None):
            tag.tag_jobs(jobs)
        self.assertEqual(jobs[0]["job_type"], "job")


class CircuitBreakerTests(unittest.TestCase):
    """A dead CLI (present binary, every call fails) must trip the breaker
    instead of fanning out doomed subprocess calls per batch."""

    def _many_jobs(self, n: int) -> list[dict]:
        return [{"title": f"Analyst {i}", "company": "GS", "location": "London"}
                for i in range(n)]

    def test_dead_cli_trips_breaker_and_stops_calling(self):
        # Enough jobs to fill more than THRESHOLD outer batches.
        n = tag.BATCH_SIZE * (tag.CIRCUIT_BREAKER_THRESHOLD + 3)
        jobs = self._many_jobs(n)

        calls = {"n": 0}

        def dead_batch(batch, bin_path, health=None):
            # Simulate an expired-OAuth CLI: present, but every call blanks.
            calls["n"] += 1
            for j in batch:
                tag._blank_tags(j)

        with patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "_tag_batch", side_effect=dead_batch):
            tag.tag_jobs(jobs)

        # (a) breaker tripped
        self.assertTrue(tag.LAST_RUN_HEALTH["cli_down"])
        # (b) no calls past the tripping batch: exactly THRESHOLD outer batches
        # ran (the retry path never fires because each batch is fully blank, and
        # the breaker trips before any retry on the THRESHOLD-th batch).
        self.assertEqual(calls["n"], tag.CIRCUIT_BREAKER_THRESHOLD)
        # (c) every job still comes back blank-tagged, not raised.
        self.assertTrue(all(j["area"] == "" for j in jobs))
        self.assertTrue(all(j["job_type"] == "job" for j in jobs))
        self.assertEqual(tag.LAST_RUN_HEALTH["jobs_tagged"], 0)

    def test_retry_noop_once_breaker_tripped(self):
        # _retry_batch must respect the breaker directly, even if reached.
        batch = self._many_jobs(3)
        for j in batch:
            tag._blank_tags(j)
        health = tag._fresh_health()
        health["cli_down"] = True
        with patch.object(tag, "_tag_batch",
                          side_effect=AssertionError("should not call CLI")):
            tag._retry_batch(batch, "/fake/claude", health)  # must not raise

    def test_healthy_cli_never_trips(self):
        jobs = self._many_jobs(tag.BATCH_SIZE * 4)

        def ok_batch(batch, bin_path, health=None):
            for j in batch:
                j.update(_MARKETS_TAGS)

        with patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "_tag_batch", side_effect=ok_batch):
            tag.tag_jobs(jobs)
        self.assertFalse(tag.LAST_RUN_HEALTH["cli_down"])
        self.assertEqual(tag.LAST_RUN_HEALTH["jobs_tagged"], len(jobs))


class TestApiFallback(unittest.TestCase):
    """When the breaker trips and ANTHROPIC_TAG_API_KEY is set, the run must
    switch to the direct-API transport instead of blanking the rest."""

    def _many_jobs(self, n: int) -> list[dict]:
        return [{"title": f"Analyst {i}", "company": "GS", "location": "London"}
                for i in range(n)]

    def test_fallback_rescues_run_after_breaker(self):
        n = tag.BATCH_SIZE * (tag.CIRCUIT_BREAKER_THRESHOLD + 3)
        jobs = self._many_jobs(n)

        def dead_batch(batch, bin_path, health=None):
            for j in batch:
                tag._blank_tags(j)

        def api_ok(batch, api_key, health=None):
            assert api_key == "sk-test"
            for j in batch:
                j.update(_MARKETS_TAGS)

        with patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "_api_key", return_value="sk-test"), \
             patch.object(tag, "_tag_batch", side_effect=dead_batch), \
             patch.object(tag, "_tag_batch_api", side_effect=api_ok):
            tag.tag_jobs(jobs)

        self.assertTrue(tag.LAST_RUN_HEALTH["api_fallback"])
        self.assertFalse(tag.LAST_RUN_HEALTH["cli_down"])
        # The tripping batch is re-run on the API and every batch after it
        # rides the API too; only the pre-trip batches stay blank for the
        # nightly hook.
        blank = sum(1 for j in jobs if j["area"] == "")
        self.assertEqual(
            blank, tag.BATCH_SIZE * (tag.CIRCUIT_BREAKER_THRESHOLD - 1))

    def test_dead_api_still_trips_breaker(self):
        n = tag.BATCH_SIZE * (tag.CIRCUIT_BREAKER_THRESHOLD + 2)
        jobs = self._many_jobs(n)

        def dead(batch, *a, **kw):
            for j in batch:
                tag._blank_tags(j)

        with patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "_api_key", return_value="sk-test"), \
             patch.object(tag, "_tag_batch", side_effect=dead), \
             patch.object(tag, "_tag_batch_api", side_effect=dead):
            tag.tag_jobs(jobs)

        self.assertTrue(tag.LAST_RUN_HEALTH["api_fallback"])
        self.assertTrue(tag.LAST_RUN_HEALTH["cli_down"])
        self.assertTrue(all(j["area"] == "" for j in jobs))


if __name__ == "__main__":
    unittest.main()
