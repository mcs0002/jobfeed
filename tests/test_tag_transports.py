"""The two HTTP tagging transports, run for real with only the network faked.

tests/test_tag.py replaces _tag_batch_api with a trap so no test can bill the
Messages API from the M1, which also meant no test ever ran the function: it is
the fallback that carries a run when the CLI's OAuth dies. Here _post_json is
faked instead, so the request each transport builds and the way it reads the
reply are both checked, and the network is still unreachable.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobfeed import tag

ROW = "0|markets|trading|graduate|job|London|United Kingdom|Europe|onsite|-|0|master|-"
ROW1 = "1|ibd|-|analyst|internship|Paris|France|Europe|hybrid|fr|0|-|2027-06"


def _jobs(n):
    return [{"title": f"Analyst {i}", "company": "GS", "location": "London"}
            for i in range(n)]


class _Isolated(unittest.TestCase):
    def setUp(self):
        guards = [
            patch.dict(os.environ, {"TAG_RUBRIC_ADDENDUM": ""}, clear=False),
            patch.object(tag, "ROOT", tempfile.mkdtemp(prefix="tagtest-")),
            patch.object(tag, "_log_debug"),
            patch.object(tag, "_API_DEAD", False),
        ]
        for g in guards:
            g.start()
            self.addCleanup(g.stop)
        self.health = tag._fresh_health()

    def _post(self, response=None, error=None):
        calls = []

        def fake(url, *, headers, body, label):
            calls.append({"url": url, "headers": headers, "body": body})
            if error:
                raise error
            return response
        p = patch.object(tag, "_post_json", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return calls


class AnthropicFallbackTests(_Isolated):
    def test_request_carries_key_model_and_cached_rubric(self):
        calls = self._post({"content": [{"type": "text", "text": ROW}]})
        tag._tag_batch_api(_jobs(1), "sk-test", health=self.health)
        (call,) = calls
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], "sk-test")
        self.assertIn("anthropic-version", call["headers"])
        body = call["body"]
        self.assertEqual(body["model"], tag.MODEL)
        self.assertEqual(body["system"][0]["text"], tag._SYSTEM)
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertIn("Analyst 0", body["messages"][0]["content"])

    def test_text_blocks_are_parsed_and_other_blocks_ignored(self):
        self._post({
            "content": [
                {"type": "thinking", "thinking": "9|junk"},
                {"type": "text", "text": ROW + "\n"},
                {"type": "text", "text": ROW1},
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_read_input_tokens": 90},
        })
        jobs = _jobs(2)
        tag._tag_batch_api(jobs, "sk-test", health=self.health)
        self.assertEqual(jobs[0]["area"], "markets")
        self.assertEqual(jobs[0]["desk"], "trading")
        self.assertEqual(jobs[1]["area"], "ibd")
        self.assertEqual(jobs[1]["job_type"], "internship")
        h = self.health
        self.assertEqual((h["batches_total"], h["batches_ok"], h["batches_failed"]),
                         (1, 1, 0))
        self.assertEqual((h["tokens_in"], h["tokens_out"], h["tokens_cached"]),
                         (100, 20, 90))

    def test_transport_failure_blanks_the_batch_and_records_why(self):
        self._post(error=ConnectionError("host down"))
        jobs = _jobs(2)
        tag._tag_batch_api(jobs, "sk-test", health=self.health)  # must not raise
        self.assertTrue(all(tag._is_untagged(j) for j in jobs))
        self.assertEqual(self.health["batches_failed"], 1)
        self.assertIn("host down", self.health["failure_reasons"][0])


class OpenAICompatibleTests(_Isolated):
    CFG = {"base_url": "https://llm.example/v1", "api_key": "k", "model": "m",
           "max_tokens": 1000, "extra": {}, "format": "json_rows"}

    def _reply(self, rows, **usage):
        return {"choices": [{"message": {"content": json.dumps({"rows": rows})}}],
                "usage": usage}

    def test_structured_request_pins_one_row_per_role(self):
        calls = self._post(self._reply([ROW.split("|"), ROW1.split("|")]))
        cfg = {**self.CFG, "extra": {"temperature": None, "thinking": {"type": "off"}}}
        tag._tag_batch_openai(_jobs(2), cfg, health=self.health)
        (call,) = calls
        self.assertEqual(call["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer k")
        body = call["body"]
        self.assertNotIn("temperature", body)        # null removes a default
        self.assertEqual(body["thinking"], {"type": "off"})  # extras override
        fmt = body["response_format"]["json_schema"]
        self.assertTrue(fmt["strict"])
        rows = fmt["schema"]["properties"]["rows"]
        self.assertEqual((rows["minItems"], rows["maxItems"]), (2, 2))

    def test_structured_reply_tags_the_batch(self):
        self._post(self._reply([ROW.split("|"), ROW1.split("|")],
                               prompt_tokens=50, completion_tokens=5,
                               prompt_tokens_details={"cached_tokens": 40}))
        jobs = _jobs(2)
        tag._tag_batch_openai(jobs, self.CFG, health=self.health)
        self.assertEqual([j["area"] for j in jobs], ["markets", "ibd"])
        self.assertEqual(self.health["tokens_cached"], 40)
        self.assertFalse(tag._api_transport_is_dead())

    def test_alternate_cache_counter_spelling_is_counted(self):
        self._post(self._reply([ROW.split("|")], prompt_cache_hit_tokens=7))
        tag._tag_batch_openai(_jobs(1), self.CFG, health=self.health)
        self.assertEqual(self.health["tokens_cached"], 7)

    def test_wrong_row_count_blanks_but_keeps_transport_alive(self):
        # A malformed answer is a parse failure, not a dead host: the run must
        # not abandon a working provider over one bad batch.
        self._post(self._reply([ROW.split("|")]))
        jobs = _jobs(2)
        tag._tag_batch_openai(jobs, self.CFG, health=self.health)
        self.assertTrue(all(tag._is_untagged(j) for j in jobs))
        self.assertFalse(tag._api_transport_is_dead())

    def test_transport_failure_marks_provider_dead(self):
        self._post(error=ConnectionError("402 no credit"))
        jobs = _jobs(1)
        tag._tag_batch_openai(jobs, self.CFG, health=self.health)
        self.assertTrue(tag._api_transport_is_dead())
        self.assertTrue(tag._is_untagged(jobs[0]))
        self.assertEqual(self.health["batches_failed"], 1)


class StructuredRowTests(unittest.TestCase):
    def test_fields_are_cleaned_before_the_line_parser(self):
        row = ROW.split("|")
        row[5], row[9], row[12] = "New\nYork", None, "a|b"
        line = tag._json_to_pipe(json.dumps({"rows": [row]}), 1)
        fields = line.split("|")
        self.assertEqual(len(fields), 13)
        self.assertEqual((fields[5], fields[9], fields[12]), ("New York", "-", "a/b"))

    def test_object_rows_follow_field_order(self):
        obj = dict(zip(tag._FIELD_ORDER, ROW.split("|")))
        shuffled = dict(reversed(list(obj.items())))
        self.assertEqual(tag._json_to_pipe(json.dumps({"rows": [shuffled]}), 1), ROW)

    def test_bare_list_is_accepted(self):
        self.assertEqual(tag._json_to_pipe(json.dumps([ROW.split("|")]), 1), ROW)

    def test_malformed_rows_are_rejected(self):
        cases = {
            "count": ({"rows": [ROW.split("|")]}, 2),
            "fields": ({"rows": [ROW.split("|")[:12]]}, 1),
            "index": ({"rows": [ROW1.split("|")]}, 1),
            "not a list": ({"rows": "x"}, 1),
        }
        for name, (payload, n) in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                tag._json_to_pipe(json.dumps(payload), n)

    def test_object_schema_is_closed_and_complete(self):
        schema = tag._json_schema("json_obj", 3)
        rows = schema["properties"]["rows"]
        item = rows["items"]
        self.assertEqual((rows["minItems"], rows["maxItems"]), (3, 3))
        self.assertEqual(item["required"], list(tag._FIELD_ORDER))
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(item["properties"]["area"]["enum"], sorted(tag.AREAS))
        self.assertIn("-", item["properties"]["desk"]["enum"])
        self.assertFalse(schema["additionalProperties"])

    def test_row_schema_pins_thirteen_fields(self):
        item = tag._json_schema("json_rows", 1)["properties"]["rows"]["items"]
        self.assertEqual((item["minItems"], item["maxItems"]), (13, 13))


if __name__ == "__main__":
    unittest.main()
