import unittest

from filter import has_experience_wall, is_relevant


class ExperienceWallTests(unittest.TestCase):
    def test_macquarie_minimum_3_plus_years(self):
        text = (
            "Proven experience in algorithmic trading within intraday power "
            "markets (minimum 3+ years). Strong quantitative and programming "
            "skills, with proficiency in Python, C++."
        )
        wall, years = has_experience_wall(text)
        self.assertTrue(wall)
        self.assertEqual(years, 3)

    def test_engie_at_least_5_years(self):
        text = (
            "Education Level: Master's Degree. At least 5 years' experience of "
            "working in a trading environment, focusing on advanced model dev."
        )
        wall, years = has_experience_wall(text)
        self.assertTrue(wall)
        self.assertEqual(years, 5)

    def test_jpmc_plus_3_years_experience(self):
        text = (
            "Master's Degree in Finance, Mathematics, Engineering plus 3 years "
            "of experience in the job offered or as US Flow Trader."
        )
        wall, years = has_experience_wall(text)
        self.assertTrue(wall)
        self.assertEqual(years, 3)

    def test_glencore_0_to_3_years_does_not_trigger(self):
        text = (
            "Master's favourable 0-3 years' professional experience in market "
            "risk, risk analytics, trading support, or a related field."
        )
        wall, _ = has_experience_wall(text)
        self.assertFalse(wall)

    def test_company_age_boilerplate_does_not_trigger(self):
        text = (
            "We are a global financial services group operating in 30 markets "
            "with 57 years of unbroken profitability. At Macquarie..."
        )
        wall, _ = has_experience_wall(text)
        self.assertFalse(wall)

    def test_empty_description(self):
        wall, years = has_experience_wall("")
        self.assertFalse(wall)
        self.assertEqual(years, 0)

    def test_german_mindestens(self):
        text = "Wir suchen Kandidaten mit mindestens 5 Jahre Berufserfahrung im Trading."
        wall, years = has_experience_wall(text)
        self.assertTrue(wall)
        self.assertEqual(years, 5)

    def test_french_au_moins(self):
        text = "Vous justifiez d'au moins 5 ans d'expérience en salle de marchés."
        wall, years = has_experience_wall(text)
        self.assertTrue(wall)
        self.assertEqual(years, 5)

    def test_below_threshold_does_not_trigger(self):
        text = "Minimum 1 year of relevant experience required."
        wall, _ = has_experience_wall(text)
        self.assertFalse(wall)

    def test_bare_5_years_without_experience_context_does_not_trigger(self):
        text = "Our trading desk has been operational for 5 years across Asia."
        wall, _ = has_experience_wall(text)
        self.assertFalse(wall)

    def test_incommodities_range_lower_bound_walls(self):
        # Regression: InCommodities "Natural Gas Fundamental Analyst"
        # (gen_24ab91507ac95129) phrases the requirement as a range —
        # "You have 3-5 years of experience". has_experience_wall previously saw
        # only explicit-minimum + N+ phrasings, so this fell through to
        # min_yoe=0 despite being a hard 3y wall. The range's LOWER bound (3) is
        # the real minimum.
        text = "You have 3-5 years of experience within natural gas fundamentals."
        wall, years = has_experience_wall(text)
        self.assertTrue(wall)
        self.assertEqual(years, 3)

    def test_range_en_dash_and_to_variants_wall_on_lower(self):
        for text in (
            "5–7 years of experience in structured credit.",   # en-dash
            "4 to 6 years of relevant experience required.",   # 'to'
            "3 bis 6 Jahre Berufserfahrung im Trading.",       # German 'bis'
        ):
            wall, years = has_experience_wall(text)
            self.assertTrue(wall, text)
            self.assertIn(years, (5, 4, 3), text)

    def test_range_lower_below_threshold_stays_no_wall(self):
        # "0-3 years" / "2-4 years" are entry-level windows — lower bound < 3.
        for text in (
            "0-3 years of experience in a related field.",
            "2-4 years of experience welcomed.",
        ):
            wall, _ = has_experience_wall(text)
            self.assertFalse(wall, text)

    def test_cross_sentence_experience_does_not_trigger(self):
        # H2 false positive: the "5 years" (company age) and the "experience"
        # token live in DIFFERENT sentences, so the year count must not be read
        # as a YoE wall just because it fell inside the ±80-char window.
        text = (
            "Our desk was established over 5 years ago. Prior experience is a "
            "plus but not required for graduates."
        )
        wall, _ = has_experience_wall(text)
        self.assertFalse(wall)

    def test_founded_years_ago_then_separate_experience_sentence(self):
        text = (
            "Founded 5 years ago, we are a fast-growing trading firm. "
            "Experience with Excel and Python is welcome."
        )
        wall, _ = has_experience_wall(text)
        self.assertFalse(wall)

    def test_genuine_walls_still_detected(self):
        for text, expected in (
            ("We require a minimum of 5 years' experience in trading.", 5),
            ("5+ years of relevant experience is essential.", 5),
            ("Candidates need at least 3 years experience in trading desks.", 3),
        ):
            wall, years = has_experience_wall(text)
            self.assertTrue(wall, text)
            self.assertEqual(years, expected, text)


class RelevancePassThroughTests(unittest.TestCase):
    """Negative-only filter — every job passes unless it hits a drop bucket.
    These titles should reach the LLM."""

    def test_graduate_markets_analyst_passes(self):
        self.assertTrue(is_relevant({"title": "Graduate Markets Analyst"}))

    def test_fixed_income_graduate_programme_passes(self):
        self.assertTrue(is_relevant({"title": "Fixed Income Graduate Programme"}))

    def test_junior_kreditanalyst_passes(self):
        self.assertTrue(
            is_relevant({"title": "Junior Kreditanalyst Immobilienfinanzierung"})
        )

    def test_capital_markets_analyst_passes_to_llm(self):
        # Ambiguous title (could be FO markets or IBD/DCM) — LLM judges,
        # by default it passes through to be tagged.
        self.assertTrue(is_relevant({"title": "Capital Markets Analyst"}))

    def test_compliance_analyst_markets_passes_to_llm(self):
        # "compliance" alone is NOT a drop — markets compliance exists.
        self.assertTrue(is_relevant({"title": "Compliance Analyst — Markets"}))

    def test_risk_analyst_counterparty_credit_passes_to_llm(self):
        # "risk" alone is NOT a drop — market risk = FO, op risk = BO.
        self.assertTrue(is_relevant({"title": "Risk Analyst — Counterparty Credit"}))

    def test_plain_trader_passes(self):
        self.assertTrue(is_relevant({"title": "Trader"}))

    def test_dual_rung_analyst_associate_passes(self):
        # "associate" is in SENIOR_TITLE_DROPS but _DUAL_RUNG_RE bypasses it
        # for dual-rung titles since the user applies at the analyst rung.
        self.assertTrue(
            is_relevant({"title": "Equity Research — Analyst / Associate"})
        )


class PreDegreeDropsTests(unittest.TestCase):
    """Pre-degree / school-level programmes are ALWAYS dropped (the user holds
    a bachelor's, starting a master's) — but university internships, graduate
    schemes, French alternance/apprentissage, and degree/graduate
    apprenticeships stay in scope."""

    def test_drops_school_internship(self):
        self.assertFalse(is_relevant({"title": (
            "School Internship in Management Consulting: Industrials & "
            "Services Industry (2026-2027)")}))

    def test_drops_apprentice_program(self):
        self.assertFalse(is_relevant({"title": "Brazil - Apprentice Program"}))

    def test_drops_ausbildung(self):
        self.assertFalse(is_relevant({"title": "Ausbildung Bankkaufmann (m/w/d)"}))

    def test_drops_schuelerpraktikum(self):
        self.assertFalse(is_relevant({"title": "Schülerpraktikum bei Deutsche Bank"}))

    def test_drops_work_experience_and_sixth_form(self):
        self.assertFalse(is_relevant({"title": "Work Experience Programme - Spring"}))
        self.assertFalse(is_relevant({"title": "Sixth Form Insight Day"}))

    def test_keeps_degree_and_graduate_apprenticeship(self):
        self.assertTrue(is_relevant({"title": "Degree Apprenticeship - Global Markets"}))
        self.assertTrue(is_relevant({"title": "Graduate Apprenticeship, Technology"}))

    def test_keeps_french_alternance_and_apprenti(self):
        # FR Bac+5 work-study — wanted; must not be caught by the apprentice gate.
        self.assertTrue(is_relevant({"title": "Alternance - Analyste Asset Management H/F"}))
        self.assertTrue(is_relevant({"title": "Apprenti(e) Gestion d'actifs H/F"}))

    def test_keeps_french_english_titled_apprentice_with_hf(self):
        # BNP renders FR alternance with the English word "Apprentice" + H/F —
        # the French tell must exempt it from the apprentice drop.
        self.assertTrue(is_relevant(
            {"title": "Portfolio Manager Apprentice - Alternative Credit H/F"}))

    def test_drops_uk_vocational_apprentice(self):
        # No degree/graduate/French signal → non-degree apprenticeship, dropped.
        self.assertFalse(is_relevant({"title": "Investment Operations Apprentice"}))
        self.assertFalse(is_relevant({"title": "Corporate Banking Operations - Apprenticeship"}))

    def test_keeps_university_internship_and_werkstudent(self):
        self.assertTrue(is_relevant({"title": "Off-Cycle Internship - Telecoms Team"}))
        self.assertTrue(is_relevant({"title": "Werkstudent Portfolio Management (m/w/d)"}))


class LocationDropsTests(unittest.TestCase):
    def test_drops_india_country(self):
        self.assertFalse(
            is_relevant({"title": "Markets Analyst", "location": "Bengaluru, India"})
        )

    def test_drops_mumbai(self):
        self.assertFalse(
            is_relevant({"title": "Trader", "location": "Mumbai"})
        )

    def test_keeps_indiana_not_swallowed_by_india(self):
        # Word-boundary match: 'india' must not drop US Midwest roles.
        self.assertTrue(
            is_relevant({"title": "Markets Analyst", "location": "Indianapolis, IN"})
        )
        self.assertTrue(
            is_relevant({"title": "Markets Analyst", "location": "Indiana, US"})
        )

    def test_drops_manila(self):
        self.assertFalse(
            is_relevant({"title": "Quant Analyst", "location": "Manila, Philippines"})
        )

    def test_drops_warsaw(self):
        self.assertFalse(
            is_relevant({"title": "Markets Analyst", "location": "Warsaw, Poland"})
        )

    def test_keeps_china_locations(self):
        # China is intentionally NOT in LOCATION_DROPS.
        for loc in ("Shanghai", "Beijing", "Shenzhen", "Hong Kong"):
            self.assertTrue(
                is_relevant({"title": "Markets Analyst", "location": loc}),
                f"expected pass for {loc}",
            )


class SeniorTitleDropsTests(unittest.TestCase):
    def test_drops_senior_credit_analyst(self):
        self.assertFalse(is_relevant({"title": "Senior Credit Analyst"}))

    def test_drops_assistant_vice_president(self):
        self.assertFalse(is_relevant({
            "title": "Capital Management Senior Analyst - Assistant Vice President"
        }))

    def test_drops_avp_abbreviation(self):
        self.assertFalse(is_relevant({"title": "Capital Markets Strategy, AVP"}))

    def test_drops_german_team_lead(self):
        self.assertFalse(is_relevant({"title": "Teamleiter Kreditanalyse"}))

    def test_drops_german_group_lead(self):
        self.assertFalse(
            is_relevant({"title": "Gruppenleitung Credit Risk Management"})
        )

    def test_keeps_solo_associate_by_default(self):
        # Associate is now STORED by default (firm-dependent rung: entry at
        # PE/AM/consulting). The web app's "Include associate roles" toggle and
        # the seniority tag sort it; it is no longer dropped at scrape time.
        self.assertTrue(is_relevant({"title": "Associate, Credit Trading"}))

    def test_strict_mode_drops_solo_associate(self):
        # Under JOBS_STRICT_FILTER the bank-senior reading still applies.
        self.assertFalse(
            is_relevant({"title": "Associate, Credit Trading"}, strict=True)
        )

    def test_strict_mode_keeps_dual_rung_associate(self):
        self.assertTrue(
            is_relevant({"title": "Equity Research — Analyst / Associate"}, strict=True)
        )

    def test_drops_undotted_sr_abbreviation(self):
        # Regression: the 'sr ' term's trailing space used to demand a
        # space + NON-word char, so 'Sr Analyst' never matched.
        self.assertFalse(is_relevant({"title": "Sr Analyst, Credit Risk"}))

    def test_drops_md_title(self):
        self.assertFalse(is_relevant({"title": "MD Fixed Income Sales"}))

    def test_keeps_md_ampersand_compound(self):
        # 'MD&A' (Management Discussion & Analysis) must not hit the 'md '
        # senior marker — '&' is excluded from the boundary.
        self.assertTrue(is_relevant({"title": "MD&A Analyst"}))


class TitleYoeRangeTests(unittest.TestCase):
    """Title-level YoE gate must judge ranges by their LOWER bound — the
    regex only sees the number adjacent to 'years', i.e. the upper bound,
    which used to hard-drop entry-level windows like '0-3 years'."""

    def test_keeps_entry_level_range(self):
        self.assertTrue(is_relevant({"title": "Analyst (0-3 years experience)"}))

    def test_keeps_two_to_four_range(self):
        self.assertTrue(is_relevant({"title": "Credit Analyst 2 to 4 years"}))

    def test_drops_range_with_senior_lower_bound(self):
        self.assertFalse(is_relevant({"title": "Trader (3-5 years experience)"}))

    def test_drops_plain_wall(self):
        self.assertFalse(is_relevant({"title": "Analyst, 5 years experience"}))


# The function-level buckets only drop in STRICT mode now (the default stores
# the rest and tags it for site-side filtering). These assert strict mode still
# works; RelaxedDefaultTests below asserts the new default passes them through.
class BackOfficeDropsTests(unittest.TestCase):
    def test_drops_kyc_analyst(self):
        self.assertFalse(is_relevant({"title": "KYC Analyst"}, strict=True))

    def test_drops_trade_support(self):
        self.assertFalse(
            is_relevant({"title": "Trade Support Analyst — Global Markets"}, strict=True)
        )

    def test_drops_middle_office(self):
        self.assertFalse(is_relevant({"title": "Middle Office Analyst"}, strict=True))

    def test_drops_fund_accounting(self):
        self.assertFalse(is_relevant({"title": "Fund Accounting Analyst"}, strict=True))


class TechDropsTests(unittest.TestCase):
    def test_drops_software_engineer(self):
        self.assertFalse(is_relevant({"title": "Graduate Software Engineer"}, strict=True))

    def test_drops_security_engineer(self):
        self.assertFalse(is_relevant({"title": "Security Assurance Engineer"}, strict=True))

    def test_drops_data_engineer(self):
        self.assertFalse(is_relevant({"title": "Data Engineer — Trading"}, strict=True))


class NonFinanceDropsTests(unittest.TestCase):
    def test_drops_recruiter(self):
        self.assertFalse(is_relevant({"title": "Talent Acquisition Partner"}, strict=True))

    def test_drops_marketing(self):
        self.assertFalse(is_relevant({"title": "Marketing Manager EMEA"}, strict=True))

    def test_drops_internal_audit(self):
        self.assertFalse(is_relevant({"title": "Internal Audit Analyst"}, strict=True))


class IBDDropsTests(unittest.TestCase):
    def test_drops_ma(self):
        self.assertFalse(is_relevant({"title": "M&A Analyst"}, strict=True))

    def test_drops_ibd_analyst(self):
        self.assertFalse(is_relevant({"title": "Investment Banking Analyst"}, strict=True))

    def test_drops_leveraged_finance(self):
        self.assertFalse(is_relevant({"title": "Leveraged Finance Analyst"}, strict=True))


class RelaxedDefaultTests(unittest.TestCase):
    """Default mode (negative-scope, June 2026): the unambiguous-noise buckets
    (back-office, retail, tech, non-finance) are dropped ALWAYS so the heavy
    executor can pull full bank boards. Every finance division — including IBD —
    passes by default and is tagged for the web app. Hard gates still drop."""

    def test_back_office_dropped_by_default(self):
        self.assertFalse(is_relevant({"title": "KYC Analyst"}))
        self.assertFalse(is_relevant({"title": "Fund Accounting Analyst"}))
        self.assertFalse(is_relevant({"title": "Fund Servicing Associate"}))

    def test_retail_dropped_by_default(self):
        self.assertFalse(is_relevant({"title": "Personal Banker"}))
        self.assertFalse(is_relevant({"title": "Branch Manager"}))

    def test_ibd_passes_through(self):
        self.assertTrue(is_relevant({"title": "M&A Analyst"}))
        self.assertTrue(is_relevant({"title": "Investment Banking Analyst"}))

    def test_tech_dropped_by_default(self):
        self.assertFalse(is_relevant({"title": "Graduate Software Engineer"}))

    def test_extra_drops_per_source(self):
        # A per-source noise term drops only when supplied (e.g. a commodity
        # house's plant ops) — the global lists stay untouched.
        self.assertTrue(is_relevant({"title": "Grain Merchandiser"}))
        self.assertFalse(
            is_relevant({"title": "Grain Merchandiser"},
                        extra_drops={"grain merchandiser"})
        )

    def test_hard_gates_still_drop(self):
        # Senior and location gates apply in both modes.
        self.assertFalse(is_relevant({"title": "Senior Credit Analyst"}))
        self.assertFalse(is_relevant({"title": "Trader", "location": "Mumbai"}))

    def test_internships_stored_in_default(self):
        # Internships are browsable in the web app (stored, tagged
        # job_type=internship, hidden behind the show_internships toggle).
        self.assertTrue(is_relevant({"title": "Markets Internship Summer 2026"}))
        self.assertTrue(is_relevant({"title": "Praktikum Treasury"}))
        self.assertTrue(is_relevant({"title": "Werkstudent Treasury"}))


class InternshipDropsTests(unittest.TestCase):
    """Internships are dropped only under strict scrape mode (JOBS_STRICT_FILTER)."""

    def test_strict_drops_english_internship(self):
        self.assertFalse(is_relevant({"title": "Markets Internship Summer 2026"}, strict=True))

    def test_strict_drops_german_praktikum(self):
        self.assertFalse(is_relevant({"title": "Praktikum Treasury"}, strict=True))

    def test_strict_drops_werkstudent(self):
        self.assertFalse(is_relevant({"title": "Werkstudent Treasury"}, strict=True))


class SeniorTermSubstringCollisionTests(unittest.TestCase):
    """Regression tests for the _contains_term word-boundary fix.

    Before the fix, single-word drop terms matched as plain substrings, so
    'lead' hit 'leadership'/'leading', 'senior' hit 'seniority', etc. These
    caused irreversible pre-store drops of genuinely relevant roles, violating
    the store-broad doctrine.

    Fix: single-word terms (no space) now use a right-side word boundary
    (term\\b) which prevents prefix collisions while still catching German
    compound-word suffixes (e.g. 'leiter' matches 'teamleiter'/'projektleiter').
    Multi-word phrases keep plain substring matching.

    For 'principal' an additional exemption regex (_SENIOR_TITLE_EXEMPT_RE)
    handles the firm-name case ('Principal Investments', 'Principal Financial')
    where the word is already a standalone token but is a proper noun, not a
    seniority descriptor."""

    # --- Confirmed false drops now fixed ---

    def test_graduate_leadership_programme_passes(self):
        # 'lead' substring was matching 'leadership' → false drop. With right-
        # side boundary, 'lead\b' does not match inside 'leadership'.
        self.assertTrue(
            is_relevant({"title": "Graduate Leadership Development Programme"})
        )

    def test_analyst_principal_investments_passes(self):
        # 'principal' was matching the firm name "Principal Investments"
        # embedded in the title. _SENIOR_TITLE_EXEMPT_RE now exempts
        # 'principal invest*' firm-name patterns.
        self.assertTrue(
            is_relevant({"title": "Analyst, Principal Investments"})
        )

    def test_market_leading_firm_passes(self):
        # 'lead' substring was matching 'leading'. With right-side boundary,
        # 'lead\b' does not match inside 'leading' (next char is 'i').
        self.assertTrue(
            is_relevant({"title": "Junior Trader - Market Leading Firm"})
        )

    # --- Genuine senior titles must still drop ---

    def test_lead_software_engineer_still_drops(self):
        # 'Lead' as a standalone seniority descriptor must still fire.
        self.assertFalse(is_relevant({"title": "Lead Software Engineer"}))

    def test_team_lead_trading_still_drops(self):
        # 'Lead' at the END of a compound ("Team Lead") must still fire.
        self.assertFalse(is_relevant({"title": "Team Lead - Trading"}))

    def test_principal_engineer_still_drops(self):
        # 'Principal' as a seniority marker (before a role noun) must drop.
        self.assertFalse(is_relevant({"title": "Principal Engineer"}))

    def test_principal_analyst_still_drops(self):
        self.assertFalse(is_relevant({"title": "Principal Analyst"}))

    def test_senior_credit_analyst_still_drops(self):
        # 'senior' must still fire as a standalone word.
        self.assertFalse(is_relevant({"title": "Senior Credit Analyst"}))

    def test_seniority_label_does_not_drop(self):
        # 'senior' must NOT match inside 'seniority' (prefix collision).
        self.assertTrue(is_relevant({"title": "Seniority Ranking — Analyst Track"}))

    def test_staff_engineer_still_drops(self):
        self.assertFalse(is_relevant({"title": "Staff Engineer"}))

    def test_staffing_analyst_does_not_drop_via_staff(self):
        # 'staff' must NOT match inside 'staffing' (prefix collision).
        # (Note: 'staffing' may still be dropped by NON_FINANCE_DROPS if
        # present; this test isolates the substring-collision fix only.)
        self.assertTrue(is_relevant({"title": "Staffing Analyst Finance"}))

    def test_director_still_drops(self):
        self.assertFalse(is_relevant({"title": "Director, Fixed Income Sales"}))

    def test_directorate_does_not_drop_via_director(self):
        # 'director' must NOT match inside 'directorate'.
        self.assertTrue(is_relevant({"title": "Directorate Finance Analyst"}))

    # --- German compound-word suffix matching preserved ---

    def test_german_projektleiter_still_drops(self):
        # 'leiter' must still catch compound forms not explicitly listed,
        # e.g. 'Projektleiter', via the right-side boundary suffix match.
        self.assertFalse(is_relevant({"title": "Projektleiter Finance"}))

    def test_german_teamleiter_still_drops(self):
        # 'teamleiter' is explicitly listed and also caught by 'leiter' suffix.
        self.assertFalse(is_relevant({"title": "Teamleiter Kreditanalyse"}))


if __name__ == "__main__":
    unittest.main()
