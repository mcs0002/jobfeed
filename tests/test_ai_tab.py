"""The AI tab: a title match across every area, 'other' included, with its own
badge count that never inflates the All tab."""
import os
import tempfile
import unittest

from jobfeed.db import AI_COUNT_KEY, JobDB, ai_title


class AiTitleTests(unittest.TestCase):
    def test_matches_ai_titles_in_several_languages(self):
        for title in ("AI Engineer", "GenAI Consulting Intern", "Machine Learning Scientist",
                      "Werkstudent KI & Datenanalyse", "Spécialiste Cybersécurité IA",
                      "Data Scientist - Pricing", "Large Language Model Platform Lead",
                      "LLM Engineer", "Quant Researcher - LLMs", "Agentic SDLC Hub Engineer"):
            self.assertEqual(1, ai_title(title), title)

    def test_leaves_words_that_merely_contain_the_letters(self):
        for title in ("Maintenance Technician", "Main Street Banker", "Kiosk Assistant",
                      "Mail Room Clerk", "HTML Developer", "Tax Associate - JD/LLM", "Counsel (J.D./LL.M.)",
                      "Tax - Tax Consultant - Indirect Tax LLM - Summer 2027", ""):
            self.assertEqual(0, ai_title(title), title)


class AiTabQueryTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = JobDB(self.path)
        for jid, title, area in (("1", "AI Engineer", "other"),
                                 ("2", "Quant Researcher, Machine Learning", "quant"),
                                 ("3", "Equity Sales Analyst", "markets"),
                                 ("4", "Maintenance Technician", "other")):
            self.db.mark_seen(jid, company="Acme", title=title)
            self.db.conn.execute("UPDATE seen_jobs SET area=? WHERE id=?", (area, jid))
        self.db.conn.commit()

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.path)

    def test_ai_only_spans_areas_including_other(self):
        titles = {j["title"] for j in self.db.fetch_jobs(ai_only=True)}
        self.assertEqual({"AI Engineer", "Quant Researcher, Machine Learning"}, titles)

    def test_badge_count_is_separate_from_the_area_counts(self):
        counts = self.db.area_counts()
        self.assertEqual(2, counts[AI_COUNT_KEY])
        self.assertEqual(2, counts["other"])


if __name__ == "__main__":
    unittest.main()


class AiTitlesPassTheTechFilter(unittest.TestCase):
    """The scrape-time tech drop must not remove what the AI tab shows."""

    def test_ai_engineering_titles_are_stored(self):
        from jobfeed.filter import is_relevant
        for title in ("2027 Graduate Talent Program – AI Engineer – Zurich",
                      "2027 Graduate Talent Program – AI & Data Engineer – Zurich",
                      "Machine Learning Engineer"):
            self.assertTrue(is_relevant({"title": title, "location": "Zurich"}), title)

    def test_plain_tech_titles_still_drop(self):
        from jobfeed.filter import is_relevant
        for title in ("2027 Graduate Talent Program – Tech – Software Engineer – Zurich",
                      "Data Engineer", "Cloud Engineer"):
            self.assertFalse(is_relevant({"title": title, "location": "Zurich"}), title)
