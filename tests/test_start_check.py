"""jobfeed/start_check.py: the read-only start_date measurement."""
import unittest

from jobfeed.start_check import compare, stated_start


class StatedStartTests(unittest.TestCase):
    def test_phrasings(self):
        cases = {
            "Start date: September 2027. Apply now": "2027-09",
            "The programme starts in summer 2027": "2027-06",
            "Eintrittsdatum: 01.10.2026": "2026-10",
            "Eintritt: 1. Oktober 2026": "2026-10",
            "date de début : janvier 2027": "2027-01",
            "Starting in 2027 we offer": "2027",
            "Start date: ASAP": "asap",
            "Beginn: ab sofort": "asap",
        }
        for text, want in cases.items():
            self.assertEqual(stated_start(text)[0], want, text)

    def test_no_start_sentence(self):
        for text in ("We start early. Our 2027 intake", "Start date: flexible", ""):
            self.assertEqual(stated_start(text), ("", ""), text)

    def test_quote_stops_at_the_sentence(self):
        self.assertEqual(stated_start("Start date: September 2027. Apply now")[1],
                         "Start date: September 2027")


class CompareTests(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(compare("2027-09", "2027-09"), "agree")
        self.assertEqual(compare("2027", "2027-06"), "agree")   # year-only is not wrong
        self.assertEqual(compare("2027-06", "2027"), "agree")
        self.assertEqual(compare("2026-09", "2027-09"), "disagree")
        self.assertEqual(compare("asap", "2027-09"), "disagree")
        self.assertEqual(compare("", "2027"), "missed")
        self.assertEqual(compare("2027", ""), "unchecked")
        self.assertEqual(compare("", ""), "neither")


if __name__ == "__main__":
    unittest.main()
