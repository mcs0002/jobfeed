"""Inbox-driven CRM updates: match uniquely or not at all, and quote the reason."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import application_mail as mail  # noqa: E402
from db import JobDB  # noqa: E402

# Shapes copied from a real `mailbox --body` run on 2026-09-12.
HRT = {
    "uid": "4291", "from": "no-reply@hudson-trading.com",
    "date": "2026-09-12T13:32:03+00:00",
    "subject": "Thank you for applying to Hudson River Trading!",
    "body": "Hi Max, Thank you for your interest in Hudson River Trading! "
            "Algorithm Developers play a critical role at HRT.",
}
TRAFIGURA = {
    "uid": "4290", "from": "workday trafigura <trafigura@myworkday.com>",
    "date": "2026-09-12T12:57:11+00:00",
    "subject": "Trafigura Careers - Successful submission of Job Application",
    "body": "Dear Max the user, Thank you for your application for the role of "
            "Calgary Development Graduate Programme and for your interest in Trafigura.",
}


class ClassifyTests(unittest.TestCase):
    def test_confirmations_classify_as_applied_with_a_quote(self):
        for message in (HRT, TRAFIGURA):
            status, quote = mail.classify(message)
            self.assertEqual("applied", status, message["subject"])
            self.assertTrue(quote)
            # The quote must be text actually present in the message.
            haystack = f'{message["subject"]}. {message["body"]}'
            self.assertIn(quote.split()[0], haystack)

    def test_rejection_wins_over_a_thank_you_in_the_same_mail(self):
        status, quote = mail.classify({
            "subject": "Your application",
            "body": "Thank you for applying. Unfortunately we regret to inform you "
                    "that we are not moving forward with your application.",
        })
        self.assertEqual("rejected", status)
        self.assertIn("regret to inform", quote.lower())

    def test_german_rejection_and_assessment(self):
        self.assertEqual("rejected", mail.classify(
            {"subject": "Ihre Bewerbung", "body": "leider nicht weiter beruecksichtigen"})[0])
        # "Stellen" the verb and "Personalausweis" are not hiring context.
        self.assertEqual(("", ""), mail.classify(
            {"subject": "Zulassung", "body": "Wir stellen den Antrag, leider nicht heute. "
                                             "Bitte Personalausweis mitbringen."}))
        self.assertEqual("oa", mail.classify(
            {"subject": "Next step", "body": "Please complete the online assessment."})[0])

    def test_unrelated_mail_is_unclassified(self):
        self.assertEqual(("", ""), mail.classify(
            {"subject": "Your invoice", "body": "Payment is due next week."}))

    def test_german_regret_outside_a_hiring_context_is_not_a_rejection(self):
        # Real message from the 2026-09-12 dry run: a car-registration mail that
        # matched the rejection pattern purely on "leider nicht".
        self.assertEqual(("", ""), mail.classify(
            {"subject": "Tesla Zulassung Kennzeichen",
             "body": "leider nicht rechtzeitig"}))


class RealInboxRegressionTests(unittest.TestCase):
    """Every one of these came from the 2026-09-12 inbox and went unmatched or
    misclassified before the fixes below."""

    def rows(self):
        return [
            {"id": "wd_bp_Graduate---Supply--Trading---Shipping---Singapore--Aug-2027-_RQ114759",
             "company": "BP Supply & Trading (Early Careers)",
             "title": "Graduate - Supply, Trading & Shipping - Singapore (Aug 2027)",
             "url": "", "status": "applied"},
            {"id": "wd_bp_Graduate---Supply--trading---shipping---China--Aug-2027-_RQ115652",
             "company": "BP Supply & Trading (Early Careers)",
             "title": "Graduate - Supply, trading & shipping - China (Aug 2027)",
             "url": "", "status": "applied"},
            {"id": "campus:deutschebank|graduateprogramme", "company": "Deutsche Bank",
             "title": "Graduate Programme", "url": "", "status": "applied", "campus": True},
            {"id": "gh_8098645", "company": "Maven Securities", "title": "Graduate Trader",
             "url": "", "status": "applied"},
        ]

    def test_title_tie_break_ignores_dash_style(self):
        rows = [
            {"id": "citsec_qt_europe", "company": "Citadel Securities",
             "title": "Quantitative Trader \u2013 University Graduate (Europe)",
             "url": "", "status": "applied"},
            {"id": "citadel_flexpower", "company": "Citadel (HF)",
             "title": "Trader: Power & Renewables Trader \u2013 FlexPower (a Citadel company)",
             "url": "", "status": "applied"},
        ]
        job, _ = mail.match_job({
            "from": "No-Reply <no-reply@citadel.com>",
            "subject": "Your application for Citadel's Quantitative Trader - University "
                       "Graduate (Europe) role has been received.",
            "body": "Thank you for your interest in Citadel | Citadel Securities."}, rows)
        self.assertEqual("citsec_qt_europe", job and job["id"])

    def test_bp_rejection_wording_is_a_rejection(self):
        status, quote = mail.classify({
            "subject": "Your Application to RQ115652 - Graduate - Supply, trading & shipping",
            "body": "Thank you for your interest in our open position at bp. We regret to "
                    "advise that you do not meet the essential requirements.",
        })
        self.assertEqual("rejected", status)
        self.assertIn("regret to advise", quote.lower())

    def test_two_roles_at_one_firm_split_on_the_requisition_id(self):
        job, why = mail.match_job({
            "from": "bp Global Recruitment <donotreply@bp.com>",
            "subject": "Your Application to RQ115652 - Graduate - Supply, trading & shipping - China (Aug 2027)",
            "body": "Thank you for your interest in our open position at bp.",
        }, self.rows())
        self.assertIsNotNone(job)
        self.assertIn("RQ115652", job["id"])

    def test_a_generic_programme_title_never_qualifies_a_candidate(self):
        # "Graduate Programme" appears in half of all recruiting mail, and as a
        # standalone match it made Maven and Shell ambiguous against Deutsche Bank.
        job, why = mail.match_job({
            "from": "no-reply@mavensecurities.com",
            "subject": "Thank you for applying to Maven - Trader Graduate Programme",
            "body": "Thank you for applying.",
        }, self.rows())
        self.assertIsNotNone(job)
        self.assertEqual("gh_8098645", job["id"])

    def test_a_bare_year_is_not_a_requisition_id(self):
        rows = [{"id": "wd_x_Programme-2027", "company": "Someone Unrelated",
                 "title": "Programme", "url": "", "status": "applied"}]
        job, _ = mail.match_job({
            "subject": "Your 2027 application", "body": "Thank you for applying."}, rows)
        self.assertIsNone(job)

    def test_industry_words_do_not_identify_a_firm(self):
        rows = [{"id": "wd_shell_x", "company": "Shell Trading & Supply",
                 "title": "Shell Graduate Programme 2027", "url": "", "status": "applied"}]
        job, _ = mail.match_job({
            "subject": "An Update on Your Shell Application",
            "body": "Thank you for your interest in a career at Shell."}, rows)
        self.assertIsNotNone(job)


class MatchTests(unittest.TestCase):
    def candidates(self):
        return [
            {"id": "gh_8052050", "company": "Hudson River Trading",
             "title": "Algorithm Developer", "url": "https://hudsonrivertrading.com/x",
             "status": "queued"},
            {"id": "wd_trafigura_R-018524", "company": "Trafigura",
             "title": "Calgary Development Graduate Programme",
             "url": "https://trafigura.wd3.myworkdayjobs.com/x", "status": "queued"},
        ]

    def test_each_real_mail_matches_its_own_role(self):
        job, why = mail.match_job(HRT, self.candidates())
        self.assertEqual("gh_8052050", job["id"])
        self.assertIn("company", why)
        job, why = mail.match_job(TRAFIGURA, self.candidates())
        self.assertEqual("wd_trafigura_R-018524", job["id"])

    def test_sender_domain_need_not_equal_the_job_host(self):
        # hudson-trading.com vs hudsonrivertrading.com: host matching alone fails,
        # which is why the company name carries the match.
        self.assertIsNotNone(mail.match_job(HRT, self.candidates())[0])

    def test_two_roles_at_one_firm_are_ambiguous_unless_the_mail_names_one(self):
        rows = self.candidates() + [{
            "id": "gh_9999999", "company": "Hudson River Trading",
            "title": "Software Engineer", "url": "https://hudsonrivertrading.com/y",
            "status": "queued"}]
        # HRT's mail names the role ("Algorithm Developers play a critical
        # role"), so the title breaks the tie rather than the firm being
        # unresolvable. bp's two rejections are the case this was built for.
        job, why = mail.match_job(HRT, rows)
        self.assertEqual("gh_8052050", job["id"])
        self.assertIn("title", why)

        # Strip the role name and it is genuinely ambiguous again.
        anonymous = dict(HRT, body="Thank you for applying. We will be in touch.")
        job, why = mail.match_job(anonymous, rows)
        self.assertIsNone(job)
        self.assertIn("ambiguous", why)

    def test_short_company_names_match_whole_words_only(self):
        # "ADM" paired with a Barcelona booking and a the school receipt on 2026-09-12
        # because the token test was a substring test.
        rows = [{"id": "brassring_3354902", "company": "ADM",
                 "title": "Commercial Traineeship Program", "url": "",
                 "status": "applied"}]
        for noise in ("Booking 6252106135: offers to use at B",
                      "the school Max the user Schmidt - Payment received"):
            self.assertIsNone(mail.match_job({"subject": noise, "body": ""}, rows)[0])
        self.assertIsNotNone(mail.match_job(
            {"subject": "Thank you for your interest in ADM", "body": ""}, rows)[0])

    def test_a_source_label_suffix_is_not_part_of_the_firm_name(self):
        # The board calls it "Citadel (HF)"; Citadel's confirmation mail says
        # Citadel and never HF.
        rows = [{"id": "citadel_trader", "company": "Citadel (HF)",
                 "title": "Trader: Power & Renewables", "url": "", "status": "queued"}]
        job, _ = mail.match_job(
            {"subject": "Your application for Citadel's Trader: Power & Renewables",
             "body": "Thank you for applying."}, rows)
        self.assertIsNotNone(job)

    def test_generic_words_alone_never_match(self):
        rows = [{"id": "x", "company": "Global Capital Markets Group",
                 "title": "Analyst", "url": "", "status": "queued"}]
        job, _ = mail.match_job(
            {"subject": "Capital markets newsletter", "body": "global group"}, rows)
        self.assertIsNone(job)


class CampusTests(unittest.TestCase):
    """Graduate-programme applications have no seen_jobs row at all."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = JobDB(self.path)
        self.db.set_campus_state("dbk|grad|london", state="applied",
                                 firm="Deutsche Bank", programme="Graduate Programme")
        self.db.set_campus_state("flow|grad|ams", state="skipped",
                                 firm="Flow Traders", programme="Graduate Trading Program")

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.path)

    def test_only_ticked_programmes_are_candidates(self):
        rows = mail.candidate_campus(self.db)
        self.assertEqual(["campus:dbk|grad|london"], [r["id"] for r in rows])

    def test_a_programme_mail_is_recorded_but_changes_no_tick(self):
        message = {"uid": "1", "date": "2026-09-12T10:00:00+00:00",
                   "from": "no-reply@db.com", "subject": "Deutsche Bank application",
                   "body": "Thank you for applying to the Graduate Programme."}
        original = mail.fetch_messages
        mail.fetch_messages = lambda *a, **k: [message]
        try:
            events = mail.scan(self.db, "2026-09-01", apply=True)
        finally:
            mail.fetch_messages = original
        self.assertEqual("campus_note", events[0]["outcome"])
        self.assertEqual("campus:dbk|grad|london", events[0]["job_id"])
        # The tick is his; the scan may record beside it and never over it.
        self.assertEqual("applied",
                         self.db.all_campus_state()["dbk|grad|london"]["state"])
        listed = self.db.campus_applications()
        self.assertEqual(1, len(listed))
        self.assertEqual(1, len(listed[0]["mail"]))


class PoolTests(unittest.TestCase):
    def test_a_rejected_role_can_still_match_its_own_mail(self):
        self.assertIn("rejected", mail.CANDIDATE_STATUSES)

    def test_a_board_row_beats_the_campus_tick_for_the_same_firm(self):
        rows = [
            {"id": "gh_1", "company": "Jump Trading", "title": "Campus Quant Trader",
             "url": "", "status": "applied"},
            {"id": "campus:jump|studentsnewgrads", "company": "Jump Trading",
             "title": "Students & New Grads", "url": "", "status": "applied",
             "campus": True},
        ]
        job, why = mail.match_job({
            "from": "no-reply@jumptrading.com",
            "subject": "Thank you for applying to Jump Trading!",
            "body": "Thank you for your application."}, rows)
        self.assertEqual("gh_1", job["id"])


class ActionRequiredTests(unittest.TestCase):
    def test_an_action_mail_is_flagged_whatever_the_status(self):
        self.assertIn("pre-interview", mail.action_required({
            "subject": "BlackRock | Action required: Complete your application",
            "body": "Please complete your pre-interview assessment to progress."}).lower())
        self.assertTrue(mail.action_required({
            "subject": "Next steps", "body": "You are invited to complete an "
            "online assessment for your application."}))

    def test_a_plain_confirmation_is_not_an_action(self):
        self.assertEqual("", mail.action_required({
            "subject": "Thank you for applying to Maven",
            "body": "Thank you for your application. We will be in touch."}))

    def test_non_recruiting_mail_is_never_an_action(self):
        self.assertEqual("", mail.action_required({
            "subject": "Action required: verify your bank details",
            "body": "Please complete your profile within 3 days."}))


class ForwardOnlyTests(unittest.TestCase):
    def test_status_never_moves_backwards(self):
        self.assertTrue(mail.is_forward("queued", "applied"))
        self.assertTrue(mail.is_forward("applied", "rejected"))
        self.assertFalse(mail.is_forward("interview", "applied"))
        self.assertFalse(mail.is_forward("applied", "applied"))


class ScanTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = JobDB(self.path)
        self.db.mark_seen(
            "wd_trafigura_R-018524", company="Trafigura",
            title="Calgary Development Graduate Programme",
            url="https://trafigura.wd3.myworkdayjobs.com/x", location="Calgary",
        )
        self.db.set_status("wd_trafigura_R-018524", "queued")

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.path)

    def _scan(self, messages, apply):
        original = mail.fetch_messages
        mail.fetch_messages = lambda *a, **k: messages
        try:
            return mail.scan(self.db, "2026-09-01", apply=apply)
        finally:
            mail.fetch_messages = original

    def test_apply_moves_the_row_and_dry_run_does_not(self):
        events = self._scan([TRAFIGURA], apply=False)
        self.assertEqual("queued", events[0]["outcome"])
        self.assertEqual("queued", self.db.get_job("wd_trafigura_R-018524")["status"])
        # A dry run leaves no trace, or the next --apply would skip the message.
        self.assertFalse(self.db.application_mail_seen(events[0]["message_key"]))

        events = self._scan([TRAFIGURA], apply=True)
        self.assertEqual("applied", events[0]["outcome"])
        self.assertEqual("applied", self.db.get_job("wd_trafigura_R-018524")["status"])
        self.assertIn("Trafigura", events[0]["evidence"] + events[0]["subject"])

    def test_a_message_is_only_ever_processed_once(self):
        self._scan([TRAFIGURA], apply=True)
        self.assertEqual([], self._scan([TRAFIGURA], apply=True))

    def test_unmatched_mail_is_recorded_not_applied(self):
        events = self._scan([HRT], apply=True)
        self.assertEqual("unmatched", events[0]["outcome"])
        self.assertEqual(1, len(self.db.pending_application_mail()))


if __name__ == "__main__":
    unittest.main()
