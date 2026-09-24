"""Closing dates stated in a posting's own words (jobfeed/deadline_text.py)."""
import unittest
from datetime import date

from jobfeed.deadline_text import stated_deadline

TODAY = date(2026, 9, 24)


def when(text):
    return stated_deadline(text, today=TODAY)[0]


class StatedDeadlineTests(unittest.TestCase):
    def test_real_phrasings_from_the_gold_set(self):
        # Blackstone campus (G049) and BMO on Workday (G082), evals/tagger_gold_v1.
        self.assertEqual(when("Applications will close on Friday 30 October 2026. "
                              "Please apply early."), "2026-10-30")
        self.assertEqual(when("Application Deadline: 07/22/2027"), "2027-07-22")

    def test_equinor_submit_before_end_of_day(self):
        self.assertEqual(when("Important! To make sure your application is considered, "
                              "please submit it before the end of the day on "
                              "(dd.mm.yyyy): 07.10.2026 We encourage candidates to "
                              "apply as soon as possible."), "2026-10-07")

    def test_other_languages_and_formats(self):
        self.assertEqual(when("Bewerbungsschluss: 15.10.2026"), "2026-10-15")
        self.assertEqual(when("Bewerbungsfrist 1. Dezember 2026"), "2026-12-01")
        self.assertEqual(when("Date limite de candidature : 3 novembre 2026"), "2026-11-03")
        self.assertEqual(when("Please apply by October 5, 2026 via the portal"), "2026-10-05")
        self.assertEqual(when("Closing date 12th Nov 2026"), "2026-11-12")
        self.assertEqual(when("Deadline for applications: 2026-11-30"), "2026-11-30")

    def test_the_quote_is_the_sentence_it_came_from(self):
        _, quote = stated_deadline("We hire twice a year. Applications close "
                                   "on 30 October 2026. Good luck.", today=TODAY)
        self.assertEqual(quote, "Applications close on 30 October 2026")

    def test_quote_starts_at_the_line_or_text_start(self):
        self.assertEqual(stated_deadline("Bewerbungsschluss: 15.10.2026", today=TODAY)[1],
                         "Bewerbungsschluss: 15.10.2026")
        self.assertEqual(stated_deadline("About us\nClosing date: 1 Nov 2026", today=TODAY)[1],
                         "Closing date: 1 Nov 2026")

    def test_a_sentence_ending_in_a_year_ends_there(self):
        # The ordinal guard ("1. Dezember") must not swallow "…2026. Next".
        self.assertEqual(when("Applications close soon, latest 2026. Closing date "
                              "for the 2027 intake: 12 Nov 2026"), "2026-11-12")
        _, quote = stated_deadline("Application deadline: 30 October 2026. Apply now.",
                                   today=TODAY)
        self.assertEqual(quote, "Application deadline: 30 October 2026")

    def test_phrasings_the_llm_check_found_on_the_live_db(self):
        """Verified finds from jobfeed.deadline_llm_check, 2026-09-24: real
        postings whose stated deadline these rules used to miss."""
        cases = {
            "Application Deadline 25-Sep-2026": "2026-09-25",                          # UBS
            "This position will be open through October 11, 2026.": "2026-10-11",       # Janus Henderson
            "Application Deadline:\n2026-11-25": "2026-11-25",                          # RBC
            "Posting End Date: 30/09/2026": "2026-09-30",                               # Standard Chartered
            "feel free to send in your application today, but no later "
            "than4th of October, 2026.": "2026-10-04",                                  # SEB
            "Applications open September 9, 2026, and close November 15, 2026":
                "2026-11-15",                                                           # Evercore
            "Job Posting End Date:November-30-2026": "2026-11-30",                      # Iberdrola
            "Application Deadline:\nSunday, 11 October 2026": "2026-10-11",
        }
        for text, want in cases.items():
            self.assertEqual(when(text), want, text)

    def test_second_round_and_other_languages(self):
        """The 2026-09-24 rerun's verified misses, plus the same labels in the
        other languages the Nordic and Spanish-speaking boards use."""
        cases = {
            "Vi ser fram emot din ansökan senast den 2026-09-30 .": "2026-09-30",  # Swedbank
            "Fecha límite para apuntarse: 2026-11-10": "2026-11-10",                # BBVA
            "Recruiting for this role ends on 12/31/2026.": "2026-12-31",           # Deloitte
            "Fecha límite de postulación: 15 de octubre de 2026": "2026-10-15",
            "Plazo de postulación: 2026-10-31": "2026-10-31",
            "Søknadsfrist: 15. oktober 2026": "2026-10-15",
            "Ansøgningsfrist: 20.10.2026": "2026-10-20",
            "Sista ansökningsdag: 5 oktober 2026": "2026-10-05",
            "Sluitingsdatum: 31 oktober 2026": "2026-10-31",
            "Scadenza candidature: 30 ottobre 2026": "2026-10-30",
        }
        for text, want in cases.items():
            self.assertEqual(when(text), want, text)

    def test_third_round(self):
        """The second rerun's verified misses (2.7% of undated rows)."""
        cases = {
            "Application expected to close: 12/23/2026": "2026-12-23",   # Geneva Trading
            "Application Deadline:\n\n2026-09-28": "2026-09-28",          # RBC, blank line
            "Application Deadline:\u00a02026-10-30": "2026-10-30",         # RBC, nbsp
            "Indsend din ansøgning senest 08/10/2026.": "2026-10-08",     # Nordea, Danish
        }
        for text, want in cases.items():
            self.assertEqual(when(text), want, text)

    def test_slash_dates_read_day_first_only_after_a_european_label(self):
        self.assertEqual(when("Bewerbungsfrist: 05/11/2026"), "2026-11-05")
        self.assertEqual(when("Application Deadline: 05/11/2026"), "")
        self.assertEqual(when("Application Deadline\n\nStart date: 1 October 2026 onwards"), "")

    def test_ambiguous_dates_from_the_live_db_stay_refused(self):
        # BMO "10/04/2026" and Nordea "07/10/2026": April or October? The
        # model guessed; the rules do not.
        self.assertEqual(when("Application Deadline: 10/04/2026"), "")
        self.assertEqual(when("Submit your application no later than 07/10/2026."), "")

    def test_a_line_break_does_not_borrow_the_next_lines_date(self):
        self.assertEqual(when("Application Deadline\nStart date: 1 October 2026 onwards"), "")
        self.assertEqual(when("The role is open until filled. Start 1 October 2026."), "")

    def test_a_label_without_a_date_yields_nothing(self):
        self.assertEqual(when("Application Deadline\n\nAbout the role"), "")

    def test_meet_deadlines_boilerplate_never_matches(self):
        self.assertEqual(when("You will meet deadlines such as 30 October 2026."), "")

    def test_a_date_in_the_next_sentence_is_not_the_deadline(self):
        self.assertEqual(when("Applications are reviewed on a rolling basis; "
                              "the programme starts 1 September 2027."), "")
        self.assertEqual(when("Application deadline: rolling. Start date 1 October 2026."), "")

    def test_ambiguous_numeric_dates_are_refused(self):
        self.assertEqual(when("Closing date: 05/06/2027"), "")
        self.assertEqual(when("Closing date: 22/07/2027"), "2027-07-22")

    def test_past_far_future_and_yearless_dates_are_refused(self):
        self.assertEqual(when("Applications closed on 1 September 2026"), "")
        self.assertEqual(when("Closing date: 1 January 2029"), "")
        self.assertEqual(when("Apply by 30 October"), "")

    def test_impossible_dates_are_refused(self):
        self.assertEqual(when("Closing date: 31 February 2027"), "")

    def test_empty_input(self):
        self.assertEqual(stated_deadline("", today=TODAY), ("", ""))


if __name__ == "__main__":
    unittest.main()
