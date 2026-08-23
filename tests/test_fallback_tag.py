import unittest

from fallback_tag import classify_area, fallback_tag, parse_location


class FallbackLocationTests(unittest.TestCase):
    def test_european_location(self):
        self.assertEqual(
            parse_location("Amsterdam, North Holland, Netherlands"),
            ("Amsterdam", "Netherlands", "Europe", ""),
        )

    def test_us_state_infers_country(self):
        self.assertEqual(
            parse_location("New York, New York"),
            ("New York", "United States", "Americas", ""),
        )

    def test_city_country_and_remote(self):
        self.assertEqual(
            parse_location("Singapore (Remote)"),
            ("Singapore", "Singapore", "APAC", "remote"),
        )


class FallbackClassificationTests(unittest.TestCase):
    def test_direct_area_rules(self):
        self.assertEqual(classify_area("Quantitative Researcher"), "quant")
        self.assertEqual(classify_area("M&A Analyst"), "ibd")
        self.assertEqual(classify_area("European Bonds Trader"), "markets")

    def test_prop_trading_category_fallback(self):
        self.assertEqual(
            classify_area("Graduate Equity Analyst", "Prop Trading & Market Makers"),
            "markets",
        )

    def test_support_role_is_other(self):
        self.assertEqual(
            classify_area("Data Center Engineer", "Prop Trading & Market Makers"),
            "other",
        )

    def test_full_fallback_sets_filter_fields(self):
        job = {
            "title": "Quantitative Trading Internship (2027 Start)",
            "category": "Prop Trading & Market Makers",
            "location": "Amsterdam, North Holland, Netherlands",
            "description": "Hybrid role",
        }
        fallback_tag(job)
        self.assertEqual(job["area"], "quant")
        self.assertEqual(job["job_type"], "internship")
        self.assertEqual(job["seniority"], "intern")
        self.assertEqual(job["loc_country"], "Netherlands")
        self.assertEqual(job["loc_region"], "Europe")
        self.assertEqual(job["start_date"], "2027")
        self.assertEqual(job["work_mode"], "hybrid")


if __name__ == "__main__":
    unittest.main()
