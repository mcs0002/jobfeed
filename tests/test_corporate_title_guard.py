"""A VP-or-above rank stated in the description's "Corporate Title:" line makes
the role senior, even when the job title carries no rank (Nomura, Deutsche Bank)."""
import unittest

from jobfeed import tag


def _job(description, seniority="", title="Quantitative Structurer - Platform & AI"):
    return {"title": title, "seniority": seniority, "job_type": "job",
            "description": description}


class CorporateTitleGuardTests(unittest.TestCase):
    def test_vp_and_above_become_senior_and_survive_the_title_guard(self):
        for rank in ("Vice President / Executive Director", "Executive Director",
                     "Managing Director", "Director", "Senior Vice President"):
            j = _job(f"Job Title: X Corporate Title: {rank} Division: Global Markets")
            tag._enforce_manager(j)
            tag._enforce_corporate_title(j)
            self.assertEqual("manager", j["seniority"], rank)

    def test_lower_ranks_interns_and_plain_mentions_are_untouched(self):
        cases = [(_job("Corporate Title: Assistant Vice President"), ""),
                 (_job("Corporate Title: Associate"), ""),
                 (_job("Corporate Title: Analyst", seniority="analyst"), "analyst"),
                 (_job("Corporate Title: Vice President", seniority="intern"), "intern"),
                 (_job("You will support the Vice President of Sales."), "")]
        for j, expected in cases:
            tag._enforce_corporate_title(j)
            self.assertEqual(expected, j["seniority"], j["description"])


if __name__ == "__main__":
    unittest.main()
