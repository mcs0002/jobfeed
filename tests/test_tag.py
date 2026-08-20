"""Unit tests for tag.parse_response / coercion / tag_jobs. No CLI calls."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_cli
import tag
from tag import parse_response, _coerce

_GUARDS = []


def setUpModule():
    """Hard-stop the suite from reaching the network.

    Two paths could: tag_jobs() now calls warm_auth(), which spawns a real
    `claude`; and the circuit breaker falls back to the paid Messages API
    whenever ANTHROPIC_TAG_API_KEY is set. It IS set on the production M1, so
    running this suite there was billing the Console account and taking five
    minutes — and test_dead_cli_trips_breaker_and_stops_calling failed there
    and only there, because the fallback rescued the run the test wanted dead.

    A test that genuinely exercises the fallback patches _api_key and
    _tag_batch_api itself; an inner patch wins over these.
    """
    global _GUARDS
    _GUARDS = [
        # main.py loads the repo .env into os.environ at import time. On the
        # production M1 that includes the real DeepSeek transport, which wins
        # over the empty ROOT below. Pin a network-free baseline; individual
        # API tests override these values with their own inner patch.dict.
        patch.dict(os.environ, {
            "TAG_PROVIDER": "cli",
            "TAG_API_BASE_URL": "",
            "TAG_API_KEY": "",
            "TAG_API_MODEL": "",
            "TAG_API_EXTRA": "",
            "ANTHROPIC_TAG_API_KEY": "",
        }, clear=False),
        patch.object(tag, "warm_auth", return_value=True),
        patch.object(tag, "_api_key", return_value=""),
        patch.object(tag, "_tag_batch_api", side_effect=AssertionError(
            "a unit test reached the real Messages API")),
        # _cfg() falls back to reading the repo's .env, so on any machine that
        # has one the suite reads the OPERATOR'S live provider config and the
        # results depend on whose laptop it runs on. Pointing ROOT at an empty
        # directory makes os.environ the only config source, which is what the
        # tests actually patch. (Caught 2026-08-19: a TAG_PROVIDER line in the
        # dev .env flipped test_default_provider_is_still_the_cli to red.)
        patch.object(tag, "ROOT", tempfile.mkdtemp(prefix="tagtest-")),
    ]
    for g in _GUARDS:
        g.start()


def tearDownModule():
    for g in _GUARDS:
        g.stop()
    _GUARDS.clear()

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


class TestBackfillShapeFallback(unittest.TestCase):
    """backfill_tags calls tag_jobs() once per chunk of BATCH_SIZE rows, so each
    call sees exactly ONE batch and the per-call consecutive-blank breaker can
    never reach CIRCUIT_BREAKER_THRESHOLD. That made the API fallback
    unreachable from the nightly re-tag: when the M1's OAuth died on 2026-08-15
    the hook aborted after 3 blank chunks (39 rows) every night while the paid
    key sat unused in .env. The CLI-death flag is process-wide to close it."""

    def setUp(self):
        tag._CLI_DEAD = False

    def tearDown(self):
        tag._CLI_DEAD = False

    def _chunk(self):
        return [{"title": f"Analyst {i}", "company": "GS", "location": "London"}
                for i in range(tag.BATCH_SIZE)]

    def test_oauth_death_routes_later_chunks_to_api(self):
        class DeadProc:
            returncode = 1
            # The signature arrives on STDOUT, not stderr — which is why
            # deliver.log only ever said "unknown error".
            stdout = ("Failed to authenticate: OAuth session expired and "
                      "could not be refreshed")
            stderr = ""

        api_batches = []

        def api_ok(batch, api_key, health=None):
            assert api_key == "sk-test"
            api_batches.append(len(batch))
            for j in batch:
                j.update(_MARKETS_TAGS)

        chunks = [self._chunk() for _ in range(4)]
        with patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "warm_auth", return_value=False), \
             patch.object(tag, "_api_key", return_value="sk-test"), \
             patch.object(tag.subprocess, "run", return_value=DeadProc()), \
             patch.object(tag, "_tag_batch_api", side_effect=api_ok):
            for chunk in chunks:
                tag.tag_jobs(chunk)

        self.assertTrue(tag._cli_is_dead())
        # Every chunk lands tagged — including the first, which is recovered in
        # place by the same call that discovered the CLI was dead.
        for chunk in chunks:
            for j in chunk:
                self.assertEqual(j["area"], "markets")
        self.assertEqual(len(api_batches), len(chunks))

    def test_no_api_key_still_blanks_without_raising(self):
        class DeadProc:
            returncode = 1
            stdout = "Failed to authenticate: OAuth session expired"
            stderr = ""

        chunk = self._chunk()
        with patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "warm_auth", return_value=False), \
             patch.object(tag, "_api_key", return_value=""), \
             patch.object(tag.subprocess, "run", return_value=DeadProc()):
            tag.tag_jobs(chunk)

        self.assertTrue(all(j["area"] == "" for j in chunk))

    def test_healthy_cli_never_marks_dead(self):
        class OkProc:
            returncode = 0
            stdout = "\n".join(
                f"{i}|markets|-|analyst|job|London|United Kingdom|Europe|"
                "onsite|-|0|-|-" for i in range(tag.BATCH_SIZE))
            stderr = ""

        chunk = self._chunk()
        with patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "warm_auth", return_value=True), \
             patch.object(tag.subprocess, "run", return_value=OkProc()):
            tag.tag_jobs(chunk)

        self.assertFalse(tag._cli_is_dead())


class TestWarmAuth(unittest.TestCase):
    """One serialized refresh before any fan-out. Parallel `claude` processes
    that each refresh a rotating credential are what poisoned
    ~/.claude/.credentials.json on the M1."""

    def setUp(self):
        claude_cli._WARMED = False

    def tearDown(self):
        claude_cli._WARMED = False

    def test_warms_once_per_process(self):
        class OkProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        with patch.object(claude_cli.subprocess, "run",
                          return_value=OkProc()) as run:
            for _ in range(5):
                claude_cli.warm_auth("/fake/claude", "claude-haiku-4-5")
        self.assertEqual(run.call_count, 1)

    def test_failure_does_not_retry(self):
        with patch.object(claude_cli.subprocess, "run",
                          side_effect=OSError("boom")) as run:
            self.assertFalse(
                claude_cli.warm_auth("/fake/claude", "claude-haiku-4-5"))
            self.assertTrue(
                claude_cli.warm_auth("/fake/claude", "claude-haiku-4-5"))
        self.assertEqual(run.call_count, 1)

    def test_concurrent_callers_warm_once(self):
        import threading as _t

        class SlowProc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def slow(*a, **kw):
            time.sleep(0.05)
            return SlowProc()

        with patch.object(claude_cli.subprocess, "run",
                          side_effect=slow) as run:
            threads = [_t.Thread(target=claude_cli.warm_auth,
                                 args=("/fake/claude", "claude-haiku-4-5"))
                       for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(run.call_count, 1)


class ApiTransportTests(unittest.TestCase):
    """The third transport. Its whole point is that TAG_PROVIDER=api skips
    the CLI, so the OAuth failure mode cannot occur — these pin that it really
    does bypass it, and that a half-configured provider degrades to the old
    behaviour instead of silently tagging nothing."""

    CFG = {"TAG_PROVIDER": "api",
           "TAG_API_BASE_URL": "https://api.example.com/v1",
           "TAG_API_KEY": "sk-test",
           "TAG_API_MODEL": "test-model"}

    def _jobs(self, n):
        return [{"title": f"Analyst {i}", "company": "GS", "location": "London"}
                for i in range(n)]

    def _resp(self, n, cached=0):
        body = "\n".join(
            f"{i}|markets|trading|analyst|job|London|United Kingdom|Europe|"
            "onsite|-|0|-|-" for i in range(n))

        class R:
            status_code = 200
            @staticmethod
            def raise_for_status(): pass
            @staticmethod
            def json():
                return {"choices": [{"message": {"content": body}}],
                        "usage": {"prompt_tokens": 1000, "completion_tokens": 60,
                                  "prompt_tokens_details": {"cached_tokens": cached}}}
        return R

    def test_openai_is_primary_and_cli_is_never_called(self):
        jobs = self._jobs(tag.BATCH_SIZE)
        posted = {}

        def fake_post(url, **kw):
            posted["url"] = url
            posted["json"] = kw.get("json")
            return self._resp(len(jobs), cached=800)

        import requests
        with patch.dict(os.environ, self.CFG, clear=False), \
             patch.object(requests, "post", fake_post), \
             patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "warm_auth") as warm, \
             patch.object(tag, "_tag_batch",
                          side_effect=AssertionError("CLI must not be called")):
            tag.tag_jobs(jobs)

        self.assertTrue(posted["url"].endswith("/chat/completions"))
        # System prompt must be first and unchanged — that is the cache key.
        self.assertEqual(posted["json"]["messages"][0]["role"], "system")
        self.assertEqual(posted["json"]["messages"][0]["content"], tag._SYSTEM)
        self.assertTrue(all(j["area"] == "markets" for j in jobs))
        self.assertEqual(tag.LAST_RUN_HEALTH["tokens_in"], 1000)
        self.assertEqual(tag.LAST_RUN_HEALTH["tokens_cached"], 800)
        warm.assert_not_called()

    def test_openai_primary_works_without_claude_installed(self):
        jobs = self._jobs(3)
        import requests
        with patch.dict(os.environ, self.CFG, clear=False), \
             patch.object(requests, "post", return_value=self._resp(3)), \
             patch.object(tag, "_claude_bin", return_value=None), \
             patch.object(tag, "warm_auth") as warm, \
             patch.object(tag, "_tag_batch",
                          side_effect=AssertionError("CLI must not be called")):
            tag.tag_jobs(jobs)
        self.assertTrue(all(j["area"] == "markets" for j in jobs))
        warm.assert_not_called()

    def test_half_configured_provider_falls_back_to_cli(self):
        jobs = self._jobs(3)
        partial = dict(self.CFG); partial["TAG_API_KEY"] = ""
        used = {"cli": False}

        def cli(batch, bin_path, health=None):
            used["cli"] = True
            for j in batch:
                j.update(_MARKETS_TAGS)

        with patch.dict(os.environ, partial, clear=False), \
             patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "_tag_batch", side_effect=cli):
            tag.tag_jobs(jobs)
        self.assertTrue(used["cli"])
        self.assertTrue(any("not all set" in r
                            for r in tag.LAST_RUN_HEALTH["failure_reasons"]))

    def setUp(self):
        tag._API_DEAD = False
        tag._CLI_DEAD = False

    def tearDown(self):
        tag._API_DEAD = False
        tag._CLI_DEAD = False

    def test_dead_primary_falls_back_to_the_cli(self):
        """DeepSeek answered 402 (no credit) while TAG_PROVIDER=api was already
        live. A hard-failing PRIMARY must hand the batch to the CLI, not blank
        every tag in the run — that is the whole point of three transports."""
        jobs = self._jobs(tag.BATCH_SIZE * 2)
        import requests

        class Resp402:
            status_code = 402
            @staticmethod
            def raise_for_status():
                raise requests.HTTPError("402 Client Error: Payment Required")
            @staticmethod
            def json(): return {}

        def cli(batch, bin_path, health=None):
            for j in batch:
                j.update(_MARKETS_TAGS)

        with patch.dict(os.environ, self.CFG, clear=False), \
             patch.object(requests, "post", return_value=Resp402()), \
             patch.object(tag, "_claude_bin", return_value="/fake/claude"), \
             patch.object(tag, "_tag_batch", side_effect=cli):
            tag.tag_jobs(jobs)

        self.assertTrue(all(j["area"] == "markets" for j in jobs),
                        "every row should have been rescued by the CLI")
        self.assertTrue(tag._api_transport_is_dead())
        self.assertTrue(any("fell back to the CLI" in r
                            for r in tag.LAST_RUN_HEALTH["failure_reasons"]))

    def test_transport_error_with_no_cli_leaves_rows_blank(self):
        jobs = self._jobs(3)
        import requests
        with patch.dict(os.environ, self.CFG, clear=False), \
             patch.object(requests, "post",
                          side_effect=OSError("connection reset")), \
             patch.object(tag, "_claude_bin", return_value=None):
            tag.tag_jobs(jobs)
        self.assertTrue(all(j["area"] == "" for j in jobs))

    def test_default_provider_is_still_the_cli(self):
        with patch.dict(os.environ, {"TAG_PROVIDER": ""}, clear=False):
            self.assertEqual(tag._provider(), "cli")


class ManagerRungTests(unittest.TestCase):
    """seniority='manager' is a hard gate — the browse query filters it out —
    so a wrong manager label hides the role rather than mislabelling it."""

    def _sen(self, title, seniority):
        j = {"title": title, "seniority": seniority}
        tag._enforce_manager(j)
        return j["seniority"]

    def test_role_noun_manager_is_not_a_rung(self):
        """The class deepseek-v4-flash got wrong on 2026-08-19: it read the
        noun as the rung and would have hidden every one of these."""
        for title in ("Commercial Real Estate Asset Manager - Office",
                      "Business Banking Relationship Manager",
                      "Product Manager (13745)",
                      "Index Sales Relationship Manager (ETF)",
                      "Portfolio Manager Fixed Income"):
            with self.subTest(title=title):
                self.assertEqual(self._sen(title, "manager"), "")

    def test_managerial_rung_still_forced_up(self):
        for title in ("Manager, Financial Reporting", "Managerial Accountant"):
            with self.subTest(title=title):
                self.assertEqual(self._sen(title, "analyst"), "manager")

    def test_rung_manager_alongside_a_role_noun_still_counts(self):
        # "Manager" here is a rung even though "Portfolio Management" appears.
        self.assertEqual(
            self._sen("Manager, Portfolio Management Group", "analyst"),
            "manager")

    def test_intern_is_never_overridden(self):
        self.assertEqual(self._sen("Manager Trainee Intern", "intern"), "intern")

    def test_titles_without_manager_are_untouched(self):
        self.assertEqual(self._sen("Quantitative Analyst", "analyst"), "analyst")

    def test_manager_needs_a_marker_in_the_title(self):
        """deepseek-v4-pro called these manager on judgement alone; none say
        anything managerial, and a false manager HIDES the role."""
        for title in ("Risk Review Actuary", "Claims Analyst", "Sales, China",
                      "Legal Advisor Derivatives & Capital Markets",
                      "Editor, Digital Content", "Account Representative"):
            with self.subTest(title=title):
                self.assertEqual(self._sen(title, "manager"), "")

    def test_a_real_managerial_marker_is_respected(self):
        for title in ("Head of Trading", "Operations Supervisor",
                      "Teamleiter Kreditanalyse", "Director, Risk"):
            with self.subTest(title=title):
                self.assertEqual(self._sen(title, "manager"), "manager")


if __name__ == "__main__":
    unittest.main()


class TagRunsTelemetryTests(unittest.TestCase):
    """tag_runs.jsonl is the record that would have answered, on 2026-08-20,
    'did this run actually use the provider I configured?' without resorting to
    the mtime of .env."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tagruns-")
        self.root = patch.object(tag, "ROOT", self.tmp)
        self.root.start()
        self.addCleanup(self.root.stop)

    def _records(self):
        path = os.path.join(self.tmp, "tag_runs.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fp:
            return [json.loads(l) for l in fp if l.strip()]

    def test_records_a_run(self):
        health = tag._fresh_health()
        health.update(jobs_total=13, jobs_tagged=12, batches_total=1,
                      tokens_in=11000, tokens_cached=4224, tokens_out=350)
        tag._record_run(health)
        recs = self._records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["jobs_total"], 13)
        self.assertEqual(r["jobs_tagged"], 12)
        self.assertEqual(r["tokens_cached"], 4224)
        self.assertIn(r["provider"], ("cli", "api"))
        self.assertFalse(r["api_fallback"])

    def test_records_the_degradation_flags(self):
        health = tag._fresh_health()
        health.update(cli_down=True, api_fallback=True, api_transport_down=True)
        tag._record_run(health)
        r = self._records()[0]
        self.assertTrue(r["cli_down"])
        self.assertTrue(r["api_fallback"])
        self.assertTrue(r["api_transport_down"])

    def test_never_raises(self):
        # Telemetry must not be able to take down a tagging run.
        with patch.object(tag, "ROOT", "/nonexistent/path/for/sure"):
            tag._record_run(tag._fresh_health())  # must not raise

    def test_tag_jobs_writes_one_record_per_call(self):
        jobs = [{"id": 1, "title": "Analyst", "company": "X", "location": "NY",
                 "description": "d"}]
        with patch.object(tag, "_tag_batch_any") as batch:
            batch.side_effect = lambda b, *a, **k: [
                j.update(area="markets", job_type="job") for j in b]
            tag.tag_jobs(jobs)
        recs = self._records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["jobs_tagged"], 1)


class ManagerMarkerBreadthTests(unittest.TestCase):
    """seniority='manager' is a hard gate in the browse query, so the marker
    list decides what stays hidden. These are the cases the Review page turned
    up on 2026-08-20, when 3,237 rows were carrying a manager label the guard
    would not assign today."""

    def _seniority(self, title, start="manager"):
        j = {"title": title, "seniority": start}
        tag._enforce_manager(j)
        return j["seniority"]

    def test_leader_is_a_rung_but_lead_is_not(self):
        # \bteam\s+lead\b needs a boundary after "lead", which "Leader" denies.
        self.assertEqual(self._seniority("Team Leader Treasury Operations"), "manager")
        self.assertEqual(self._seniority("Learning Program Leader"), "manager")
        # "Lead <IC role>" is a senior individual contributor, not a manager.
        self.assertEqual(self._seniority("Lead Analyst, Credit Risk"), "")
        self.assertEqual(self._seniority("Lead Engineer"), "")

    def test_plurals(self):
        for title in ("Corporate Transformation Directors", "Team Leaders, Ops",
                      "Regional Heads of Sales"):
            self.assertEqual(self._seniority(title), "manager", title)

    def test_abbreviation_and_chief(self):
        self.assertEqual(self._seniority("Mgr, Accounting Ctrl"), "manager")
        self.assertEqual(self._seniority("Chief Risk Officer"), "manager")
        self.assertEqual(self._seniority("Global Head Markets"), "manager")

    def test_role_nouns_still_win(self):
        # A title whose only "manager" is a role noun must still be cleared —
        # broadening the marker list must not resurrect the original bug.
        for title in ("Portfolio Manager III", "Product Manager, Payments",
                      "Corporate Coverage Mexico - Relationship Manager",
                      "Asset Manager"):
            self.assertEqual(self._seniority(title), "", title)

    def test_unmarked_titles_are_cleared(self):
        for title in ("Quantitative Risk Analyst (m/f/d)", "Underwriter, Agriculture",
                      "Trade Coverage", "Hedging & Risk Advisor (Renewables)",
                      "Server-Side Engineer (C++)"):
            self.assertEqual(self._seniority(title), "", title)
