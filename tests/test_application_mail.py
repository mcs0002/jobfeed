"""Inbox-driven CRM updates: match uniquely or not at all, and quote the reason."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from applications import mail  # noqa: E402
from jobfeed.db import JobDB  # noqa: E402

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
            # These tests exercise the deterministic scan path. Without the
            # explicit flag they call the live configured API on production
            # but silently fall back on a developer machine with no key.
            return mail.scan(self.db, "2026-09-01", apply=apply, use_llm=False)
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


# The Flow Traders rejection of 2026-09-17, as `mailbox --body` returns it.
FLOW_TRADERS = {
    "uid": "5102", "from": "careers.europe@flowtraders.jobs",
    "date": "2026-09-17T11:58:00+00:00",
    "subject": "Application Update: Flow Traders",
    "body": "Hi Max the user,\n\nThank you for your interest in Flow Traders and for "
            "taking the time to complete our test.\n\nWe have reviewed your test scores "
            "and unfortunately they did not meet our global standards. At this time, we "
            "will not be able to continue with your candidacy.\n\nPlease note that we have "
            "a cool-down period of 12-months before we can accept a new application for "
            "this position.\n\nWishing you the best of luck in your job search,\n\nEmmy",
}


class CooldownTests(unittest.TestCase):
    """A stated waiting period before reapplying, found without the model.
    Missing one is silent: it looks exactly like no waiting period."""

    def test_flow_traders_twelve_month_cool_down(self):
        text = FLOW_TRADERS["subject"] + "\n" + FLOW_TRADERS["body"]
        end, quote = mail.find_cooldown(text, FLOW_TRADERS["date"])
        self.assertEqual("2027-09-17", end)
        self.assertIn("cool-down period of 12-months", quote)

    def test_number_words_and_years_count(self):
        end, _ = mail.find_cooldown("You may reapply after six months.", "2026-01-31T09:00:00+00:00")
        self.assertEqual("2026-07-31", end)
        end, _ = mail.find_cooldown("You will not be able to apply again for one year.",
                                    "2026-02-10T09:00:00+00:00")
        self.assertEqual("2027-02-10", end)

    def test_a_month_end_start_clamps_to_the_shorter_month(self):
        end, _ = mail.find_cooldown("Please wait one month before you reapply.",
                                    "2026-01-31T09:00:00+00:00")
        self.assertEqual("2026-02-28", end)

    def test_a_duration_without_a_reapply_cue_is_not_a_cool_down(self):
        end, quote = mail.find_cooldown(
            "The role requires 12 months of trading experience.", FLOW_TRADERS["date"])
        self.assertEqual(("", ""), (end, quote))

    def test_no_date_means_no_cool_down(self):
        self.assertEqual(("", ""), mail.find_cooldown(FLOW_TRADERS["body"], ""))


class CooldownScanTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = JobDB(self.path)
        self.db.mark_seen("gh_7507482", company="Flow Traders", title="Graduate Trader",
                          url="https://job-boards.greenhouse.io/flowtraders/jobs/7507482")
        self.db.set_status("gh_7507482", "oa")

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.path)

    def test_the_regex_path_rejects_and_stores_the_cool_down(self):
        original = mail.fetch_messages
        mail.fetch_messages = lambda *a, **k: [FLOW_TRADERS]
        try:
            events = mail.scan(self.db, "2026-09-17", apply=True, use_llm=False)
        finally:
            mail.fetch_messages = original
        self.assertEqual("rejected", events[0]["proposed_status"])
        self.assertEqual("rejected", self.db.get_job("gh_7507482")["status"])
        stored = self.db.application_mail_for_job("gh_7507482")[0]
        self.assertEqual("2027-09-17", stored["reapply_after"])
        self.assertIn("cool-down period of 12-months", stored["reapply_quote"])
        active = self.db.active_reapply_cooldowns("2026-09-18")
        self.assertEqual(["gh_7507482"], [c["job_id"] for c in active])
        self.assertEqual([], self.db.active_reapply_cooldowns("2027-09-18"))


class ModelReadingTests(unittest.TestCase):
    """The model's reason and cool-down are checked against the message like
    every other field it returns."""

    CANDIDATES = [{"id": "gh_7507482", "company": "Flow Traders", "title": "Graduate Trader"}]

    def _read(self, reading):
        import json
        from unittest import mock
        from applications import mail_llm as llm

        class Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": json.dumps(reading)}}]}

        cfg = {"base_url": "https://llm.test", "api_key": "k", "model": "m",
               "max_tokens": 400, "extra": {}}
        with mock.patch.object(llm.tag, "_openai_cfg", return_value=cfg), \
                mock.patch("requests.post", return_value=Resp()):
            return llm.read_message(FLOW_TRADERS, self.CANDIDATES)

    def base(self, **over):
        reading = {
            "status": "rejected", "job_id": "gh_7507482", "action": "",
            "evidence": "At this time, we will not be able to continue with your candidacy.",
            "task_key": "", "action_url": "", "deadline": "",
            "reason": "We have reviewed your test scores and unfortunately they did not "
                      "meet our global standards.",
            "reason_kind": "assessment",
            "reapply_after": "2027-09-17",
            "reapply_quote": "Please note that we have a cool-down period of 12-months "
                             "before we can accept a new application for this position.",
        }
        reading.update(over)
        return reading

    def test_a_verbatim_reason_and_cool_down_are_kept(self):
        out = self._read(self.base())
        self.assertEqual("rejected", out["status"])
        self.assertIn("test scores", out["reason"])
        self.assertEqual("assessment", out["reason_kind"])
        self.assertEqual("fixed_duration", out["reapply_kind"])
        self.assertEqual("2027-09-17", out["reapply_after"])

    def test_a_paraphrased_reason_is_blanked_but_the_rejection_stands(self):
        out = self._read(self.base(reason="He failed the numeracy test."))
        self.assertEqual("rejected", out["status"])
        self.assertEqual("", out["reason"])

    def test_a_cool_down_resolved_against_the_wrong_date_is_dropped(self):
        for bad in ("2026-09-01", "2035-01-01", "soon"):
            out = self._read(self.base(reapply_after=bad))
            self.assertEqual(("", ""), (out["reapply_after"], out["reapply_quote"]), bad)

    def test_an_invented_cool_down_sentence_is_dropped(self):
        out = self._read(self.base(reapply_quote="You may not reapply for a year."))
        self.assertEqual("", out["reapply_after"])

    def test_reason_fields_only_exist_on_a_rejection(self):
        out = self._read(self.base(status="oa", evidence="complete our test"))
        self.assertEqual(("", ""), (out["reason"], out["reason_kind"]))

    def test_academic_year_limit_is_a_rule_not_an_invented_date(self):
        bp = dict(FLOW_TRADERS,
                  date="2026-09-19T16:54:20-07:00",
                  subject="Your Application to RQ115339",
                  body="As per the guidance in the job description, only 1 application "
                       "to a bp early careers opportunity can be made per academic year.")
        original_candidate = self.CANDIDATES[0].copy()
        self.CANDIDATES[0]["company"] = "BP Supply & Trading"
        self.CANDIDATES[0]["title"] = "Trading & Analytics Graduate"
        reading = self.base(
            evidence="only 1 application to a bp early careers opportunity can be made "
                     "per academic year.",
            reason="only 1 application to a bp early careers opportunity can be made "
                   "per academic year.",
            reason_kind="screening",
            reapply_after="2027-09-19",
            reapply_quote="only 1 application to a bp early careers opportunity can be made "
                          "per academic year.")
        original = FLOW_TRADERS.copy()
        FLOW_TRADERS.update(bp)
        try:
            out = self._read(reading)
        finally:
            FLOW_TRADERS.clear()
            FLOW_TRADERS.update(original)
            self.CANDIDATES[0].clear()
            self.CANDIDATES[0].update(original_candidate)
        self.assertEqual("application_limit", out["reason_kind"])
        self.assertEqual("cycle_rule", out["reapply_kind"])
        self.assertEqual("", out["reapply_after"])
        self.assertIn("academic year", out["reapply_quote"])

    def test_same_comparative_fit_sentence_always_has_one_category(self):
        from applications import mail_llm as llm
        reasons = (
            "After careful consideration, we’ve decided to move forward with other "
            "candidates whose skills and experience more closely align with our current "
            "business needs.",
            "we have other candidates whose profiles are a closer match to our requirements.",
        )
        self.assertEqual(["comparative_fit", "comparative_fit"],
                         [llm.classify_reason_kind(r) for r in reasons])

    def test_explicit_mismatch_with_role_requirements_is_screening(self):
        from applications import mail_llm as llm
        reason = "we do not feel your experience aligns with the role requirements."
        self.assertEqual("screening", llm.classify_reason_kind(reason))


class OneRoleTests(unittest.TestCase):
    """The model path must match uniquely too. On 2026-09-17 two confirmations
    that named no role were attached to the older of two open applications."""

    GS = [
        {"id": "gs_169897_GS_CAMPUS", "company": "Goldman Sachs", "url": "",
         "title": "2027 | APEJ | Hong Kong | FICC and Equities, Sales and Trading | New Analyst",
         "location": "Hong Kong"},
        {"id": "gs_182119_GS_CAMPUS", "company": "Goldman Sachs", "url": "",
         "title": "2027 | APEJ | Hong Kong | FICC and Equities (Sales and Trading) "
                  "Quantitative Strats | New Analyst", "location": "Hong Kong"},
    ]
    DRW = [
        {"id": "gh_7957241", "company": "DRW", "title": "Quantitative Trading Analyst",
         "url": "https://job-boards.greenhouse.io/drweng/jobs/7957241", "location": "London"},
        {"id": "gh_7560394", "company": "DRW", "url": "https://job-boards.greenhouse.io/drweng/jobs/7560394",
         "title": "Junior Strategy and Operations Analyst, Flow Macro (European Market Hours)",
         "location": "Montreal"},
    ]
    CITI = [
        {"id": "talentbrew_100093431904", "company": "Citigroup", "url": "",
         "title": "Markets – Sales and Trading, Full Time Analyst, Paris – France, 2027",
         "location": "Paris, France"},
        {"id": "talentbrew_100186099392", "company": "Citigroup", "url": "",
         "title": "Markets – Sales and Trading, Full Time Analyst, London – United Kingdom, 2027",
         "location": "London, United Kingdom"},
    ]

    def mail(self, subject, body):
        return {"subject": subject, "body": body, "from": "x@example.test"}

    def test_a_confirmation_naming_no_role_does_not_pick_one(self):
        gs = self.mail("Thank You for Applying to Goldman Sachs",
                       "Thank you for your application. Our markets team will review it.")
        self.assertFalse(mail.names_one_role(self.GS[0], self.GS, gs))
        self.assertFalse(mail.names_one_role(self.GS[1], self.GS, gs))
        drw = self.mail("Thank you for applying to DRW",
                        "We received your application for an analyst role and will be in touch.")
        self.assertFalse(mail.names_one_role(self.DRW[0], self.DRW, drw))

    def test_a_mail_that_names_the_city_or_title_singles_the_role_out(self):
        paris = self.mail("Thank you for your interest in Citi!",
                          "Thank you for applying to Markets – Sales and Trading, Paris.")
        self.assertTrue(mail.names_one_role(self.CITI[0], self.CITI, paris))
        self.assertFalse(mail.names_one_role(self.CITI[1], self.CITI, paris))
        strats = self.mail("Goldman Sachs", "Your Quantitative Strats application is complete.")
        self.assertTrue(mail.names_one_role(self.GS[1], self.GS, strats))

    def test_a_title_phrase_singles_out_a_programme_of_the_same_stem(self):
        macq = [
            {"id": "avature_23275", "company": "Macquarie Group", "url": "", "location": "",
             "title": "2027 Commodities and Global Markets Summer Internship Program"},
            {"id": "avature_23733", "company": "Macquarie Group", "url": "", "location": "",
             "title": "2027 Commodities and Global Markets Graduate Programme"},
        ]
        rejection = self.mail("An update on your application | Macquarie Group",
                              "Thank you for taking the time to apply for the 2027 Commodities "
                              "and Global Markets Summer Internship Program. We are unable to "
                              "proceed with your application.")
        self.assertTrue(mail.names_one_role(macq[0], macq, rejection))
        self.assertFalse(mail.names_one_role(macq[1], macq, rejection))

    def test_a_requisition_id_singles_the_role_out(self):
        drw = self.mail("Thank you for applying to DRW", "Reference 7560394.")
        self.assertTrue(mail.names_one_role(self.DRW[1], self.DRW, drw))

    def test_the_only_role_at_a_firm_needs_no_naming(self):
        only = self.mail("Thank you for applying", "Thanks.")
        self.assertTrue(mail.names_one_role(self.DRW[0], self.DRW[:1] + self.GS, only))

    def test_the_model_path_downgrades_a_guess_to_unmatched(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = JobDB(path)
        try:
            for role in self.DRW:
                db.mark_seen(role["id"], company="DRW", title=role["title"],
                             url=role["url"], location=role["location"])
                db.set_status(role["id"], "applied")
            message = {"uid": "9", "from": "no-reply@drw.com", "date": "2026-09-17T09:02:00+00:00",
                       "subject": "Thank you for applying to DRW",
                       "body": "Thank you for your application. We will review it."}
            reading = {"status": "applied", "job_id": "gh_7957241", "action": "",
                       "evidence": "Thank you for your application.", "task_key": "",
                       "action_url": "", "deadline": "", "reason": "", "reason_kind": "",
                       "reapply_after": "", "reapply_quote": ""}
            original_fetch, original_llm = mail.fetch_messages, mail.llm_reading
            mail.fetch_messages = lambda *a, **k: [message]
            mail.llm_reading = lambda *a, **k: reading
            try:
                events = mail.scan(db, "2026-09-17", apply=True)
            finally:
                mail.fetch_messages, mail.llm_reading = original_fetch, original_llm
            self.assertEqual("unmatched", events[0]["outcome"])
            self.assertIn("model picked one of 2 DRW roles; the mail names none",
                          events[0]["match_reason"])
            self.assertEqual([], db.application_mail_for_job("gh_7957241"))
        finally:
            db.conn.close()
            os.unlink(path)


class SelfAlertTests(unittest.TestCase):
    def test_the_scrapers_own_alerts_are_never_read_as_application_mail(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = JobDB(path)
        try:
            db.mark_seen("wd_shell_R205152", company="Shell Trading & Supply",
                         title="Shell Graduate Programme 2027 - Singapore", url="https://x.test/s")
            db.set_status("wd_shell_R205152", "oa")
            alert = {"uid": "1", "from": "user@example.com",
                     "date": "2026-08-30T03:26:00+00:00",
                     "subject": "[job-scraper] 9 sources broke",
                     "body": "Shell Trading & Supply: 0 roles, application board unreachable."}
            original = mail.fetch_messages
            mail.fetch_messages = lambda *a, **k: [alert]
            try:
                self.assertEqual([], mail.scan(db, "2026-08-30", apply=True, use_llm=False))
            finally:
                mail.fetch_messages = original
            self.assertEqual([], db.application_mail_for_job("wd_shell_R205152"))
        finally:
            db.conn.close()
            os.unlink(path)


class CompanyOnlyMatchTests(unittest.TestCase):
    def test_mail_that_only_names_the_firm_stays_off_the_role(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = JobDB(path)
        try:
            db.mark_seen("bnp_hk", company="BNP Paribas",
                         title="2027 APAC Graduate Programme - Global Markets", url="https://x.test/b")
            db.set_status("bnp_hk", "oa")
            payslip = {"uid": "2", "from": "HR <no-reply@bnpparibas.com>",
                       "date": "2026-08-12T08:16:20+00:00",
                       "subject": "Ihre elektronische Gehaltsabrechnung",
                       "body": "BNP Paribas Germany: your payslip is available in E-Vault."}
            original = mail.fetch_messages
            mail.fetch_messages = lambda *a, **k: [payslip]
            try:
                events = mail.scan(db, "2026-08-12", apply=True, use_llm=False)
            finally:
                mail.fetch_messages = original
            self.assertEqual("unclassified", events[0]["outcome"])
            self.assertEqual("", events[0]["job_id"])
            self.assertEqual([], db.application_mail_for_job("bnp_hk"))
        finally:
            db.conn.close()
            os.unlink(path)


class WeeklyRoleLinkAuditTests(unittest.TestCase):
    def test_exports_only_ambiguous_multi_role_links_without_body_text(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = JobDB(path)
        try:
            roles = (
                ("acme_R123456", "Acme", "Rates Analyst", "London"),
                ("acme_R654321", "Acme", "Credit Analyst", "Paris"),
                ("solo_R111111", "Solo Firm", "Graduate Trader", "Amsterdam"),
            )
            for job_id, company, title, location in roles:
                db.mark_seen(job_id, company=company, title=title,
                             location=location, url=f"https://x.test/{job_id}")
                db.set_status(job_id, "applied")
            base = {
                "sender": "recruiting@example.test", "proposed_status": "rejected",
                "outcome": "applied", "evidence": "private body sentence",
                "match_reason": "model", "action_required": "", "task_key": "",
                "action_url": "", "action_deadline": "", "rejection_reason": "",
                "reason_kind": "", "reapply_kind": "", "reapply_after": "",
                "reapply_quote": "",
            }
            for key, subject, job_id in (
                ("generic", "Your application", "acme_R123456"),
                ("specific", "Update for R654321", "acme_R654321"),
                ("solo", "Your application", "solo_R111111"),
            ):
                db.record_application_mail(dict(
                    base, message_key=key, subject=subject, job_id=job_id,
                    received_at="2026-09-21T10:00:00+00:00",
                ))

            result = mail.audit_link_candidates(db, "2026-09-15")
            self.assertEqual(3, result["coverage"]["stored_links"])
            self.assertEqual(1, result["coverage"]["eligible"])
            self.assertEqual(1, result["coverage"]["identified_in_subject"])
            self.assertEqual(1, result["coverage"]["single_role_firm"])
            self.assertEqual(["generic"], [item["message_key"] for item in result["items"]])
            self.assertEqual("acme_R123456", result["items"][0]["assigned_id"])
            self.assertEqual(2, len(result["firms"][0]["candidates"]))
            self.assertNotIn("body", result["items"][0])
            self.assertNotIn("private body sentence", str(result))
        finally:
            db.conn.close()
            os.unlink(path)

class RecentMailTests(unittest.TestCase):
    def test_recent_mail_keeps_only_the_window_across_offsets(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = JobDB(path)
        try:
            db.mark_seen("r1", company="Geneva Trading", title="Graduate Trader", url="https://x.test/g")
            base = {"sender": "x", "subject": "s", "job_id": "r1", "evidence": "",
                    "match_reason": "", "action_required": "", "task_key": "",
                    "action_url": "", "action_deadline": "", "rejection_reason": "",
                    "reason_kind": "", "reapply_after": "", "reapply_quote": ""}
            for key, at, status, outcome in (
                    ("new", "2026-09-17T10:00:00+02:00", "rejected", "applied"),
                    ("old", "2026-09-16T07:00:00-01:00", "applied", "applied"),
                    ("junk", "2026-09-17T11:00:00+00:00", "", "unclassified")):
                db.record_application_mail(dict(base, message_key=key, received_at=at,
                                                proposed_status=status, outcome=outcome))
            keys = [m["message_key"] for m in db.recent_application_mail("2026-09-16T09:00:00+00:00")]
            self.assertEqual(["new"], keys)
        finally:
            db.conn.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()


class SenderGateTests(unittest.TestCase):
    """The two senders that produced wrong state on 2026-09-17."""

    def test_job_board_advert_is_never_application_mail(self):
        sender = "LinkedIn <jobs-noreply@linkedin.com>"
        self.assertTrue(any(h in sender.lower() for h in mail.AGGREGATOR_SENDERS))
        # The employer's own ATS must not be caught by the same net.
        rwe = '"noreply@rwe.com" <system@successfactors.eu>'
        self.assertFalse(any(h in rwe.lower() for h in mail.AGGREGATOR_SENDERS))

    def test_draft_reminder_is_not_a_stage(self):
        # Pinpoint's subject after a run saved the draft; it proposed `oa`.
        self.assertTrue(mail._DRAFT_RESUME_RE.search("Resume Your Job Application"))
        self.assertTrue(mail._DRAFT_RESUME_RE.search("Bewerbung fortsetzen"))
        # A genuine receipt and a genuine assessment invitation still pass.
        self.assertFalse(mail._DRAFT_RESUME_RE.search(
            "Application Received - 2027 Graduate Analyst Program"))
        self.assertFalse(mail._DRAFT_RESUME_RE.search(
            "Goldman Sachs: Complete Your Technical Assessment"))


class FirmTokenTests(unittest.TestCase):
    """RWE is why a length cutoff was the wrong tool."""

    def test_three_letter_firms_match_on_a_boundary(self):
        from jobfeed import db
        rwe = '"noreply@rwe.com" <system@successfactors.eu>'
        self.assertTrue(db._token_in("rwe", rwe))
        self.assertTrue(db._token_in("sig", "careers@sig.com"))
        self.assertTrue(db._token_in("ubs", '"ubs careers" <donotreply@ubs.com>'))
        # A bare substring test would have matched this one.
        self.assertFalse(db._token_in("sig", "signal processing weekly"))
        self.assertFalse(db._token_in("drw", "withdrawn application"))


class AlphanumericReqIdTests(unittest.TestCase):
    """SocGen's ids carry a letter (26000I87) and sit behind a source prefix."""

    JOB = {"id": "socgen_26000I87", "company": "Société Générale",
           "title": "TRAINEE: Trade and Sustainable Commodities", "location": "Hong Kong"}
    SIB = {"id": "socgen_26000JW2", "company": "Société Générale",
           "title": "Trainee - Prime Services Sales", "location": "Paris"}
    MAIL = {"subject": "-26000I87 at Societe Generale",
            "body": "We are reviewing all the applications."}

    def test_id_tail_is_a_requisition(self):
        self.assertIn("26000I87", mail._req_ids(self.JOB))

    def test_subject_id_names_the_role(self):
        self.assertTrue(mail.names_one_role(self.JOB, [self.JOB, self.SIB], self.MAIL))

    def test_sibling_is_not_named(self):
        self.assertFalse(mail.names_one_role(self.SIB, [self.JOB, self.SIB], self.MAIL))

    def test_word_tail_is_not_an_id(self):
        self.assertEqual(mail._req_ids({"id": "citi_ABCDEF"}), [])
