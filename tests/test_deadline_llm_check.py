"""jobfeed/deadline_llm_check.py: a model answer counts only if code verifies it."""
import unittest
from datetime import date, timedelta

from jobfeed.deadline_llm_check import verify

SOON = date.today() + timedelta(days=20)
TEXT = (f"About the role. Please send your CV by {SOON.day} "
        f"{SOON.strftime('%B')} {SOON.year} to the team.")


class VerifyTests(unittest.TestCase):
    def test_a_verbatim_quote_with_its_year_is_a_find(self):
        quote = f"send your CV by {SOON.day} {SOON.strftime('%B')} {SOON.year}"
        self.assertEqual(verify({"deadline": SOON.isoformat(), "quote": quote}, TEXT),
                         ("found", SOON.isoformat(), quote))

    def test_whitespace_and_case_are_forgiven(self):
        quote = f"SEND your  CV by {SOON.day} {SOON.strftime('%B')} {SOON.year}"
        self.assertEqual(verify({"deadline": SOON.isoformat(), "quote": quote}, TEXT)[0], "found")

    def test_an_invented_sentence_is_rejected(self):
        self.assertEqual(verify({"deadline": SOON.isoformat(),
                                 "quote": f"Applications close {SOON.year}"}, TEXT)[0],
                         "rejected:quote-not-in-text")

    def test_the_year_must_be_in_the_quote(self):
        self.assertEqual(verify({"deadline": SOON.isoformat(), "quote": "Please send your CV"},
                                TEXT)[0], "rejected:year-not-in-quote")

    def test_past_or_malformed_dates_are_rejected(self):
        past = date.today() - timedelta(days=3)
        text = f"Closing {past.isoformat()}."
        self.assertEqual(verify({"deadline": past.isoformat(),
                                 "quote": f"Closing {past.isoformat()}"}, text)[0],
                         "rejected:past-or-far")
        self.assertEqual(verify({"deadline": "next week", "quote": "x"}, TEXT)[0],
                         "rejected:bad-date")

    def test_no_deadline_and_call_failure(self):
        self.assertEqual(verify({"deadline": "", "quote": ""}, TEXT)[0], "none")
        self.assertEqual(verify(None, TEXT)[0], "error")


if __name__ == "__main__":
    unittest.main()
