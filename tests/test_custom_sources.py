import unittest
from unittest.mock import patch

from scrapers import (
    attrax,
    emply,
    beesite,
    avature,
    brassring,
    breezy,
    deshaw,
    deutscheboerse,
    directemployers,
    eightfold,
    euronext,
    generic,
    glencore,
    bundesbank,
    hibob,
    intervieweb,
    janestreet,
    jibe,
    oracle_hcm,
    phenom,
    pinpoint,
    peoplebank,
    radancy,
    rwe,
    successfactors,
    successfactors_api,
    societegenerale,
    talentbrew,
    talentsoft,
    talnet,
    teamtailor,
    uniper,
    workable,
    wellsfargo,
)


class FakeResponse:
    def __init__(self, payload=None, text="", content=b"", status_code=200):
        self.payload = payload
        self.text = text
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class GlencoreTests(unittest.TestCase):
    @patch("scrapers.glencore.make_session")
    def test_paginates_and_validates_public_careers_api(self, make_session):
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse({"totalResults": 2, "data": [{
                "id": 1,
                "jobId": "R1",
                "title": "Trading Analyst",
                "city": "Baar",
                "country": "Switzerland",
            }]}),
            FakeResponse({"totalResults": 2, "data": [{
                "id": 2,
                "jobId": "R2",
                "title": "Finance Intern",
                "city": "London",
                "country": "United Kingdom",
            }]}),
        ]

        jobs = glencore.scrape()

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], "glencore_1")
        self.assertEqual(get.call_count, 2)


class UniperTests(unittest.TestCase):
    @patch("scrapers.uniper.make_session")
    def test_paginates_first_party_filter_api(self, make_session):
        post = make_session.return_value.post
        def page(job_id, next_page):
            return FakeResponse({
                "totalHits": 2,
                "nextPage": next_page,
                "jobs": [{"data": {
                    "idClient": job_id,
                    "title": f"Role {job_id}",
                    "locations": [{"city": "Dusseldorf", "country": "Germany"}],
                    "postingDate": "2026-06-08T12:00:00",
                }}],
            })

        post.side_effect = [page("100", 1), page("101", None)]

        jobs = uniper.scrape()

        self.assertEqual(len(jobs), 2)
        self.assertIn("/101", jobs[1]["url"])


class BrassRingTests(unittest.TestCase):
    @patch("scrapers.brassring.make_session")
    def test_paginates_preloaded_search_results(self, session):
        def job(job_id):
            return {
                "Link": f"https://jobs.example/{job_id}",
                "Questions": [
                    {"QuestionName": "reqid", "Value": job_id},
                    {"QuestionName": "jobtitle", "Value": f"Role {job_id}"},
                    {"QuestionName": "formtext8", "Value": "London"},
                ],
            }

        preload = {
            "SmartSearchJSONValue": '{"KeywordCustomSolrFields":"","LocationCustomSolrFields":"","EncryptedSessionValue":"token"}',
            "TotalCount": 99,
            "searchResultsResponse": {
                "JobsCount": 2,
                "PageSize": 1,
                "Jobs": {"Job": [job("100")]},
            },
        }
        session.return_value.get.return_value = FakeResponse(text=f"""
          <input capture-escaped-parsed-value="preloadResponse"
                 value='{__import__("json").dumps(preload)}'>
          <input id="CookieValue" value="token">
        """)
        session.return_value.post.return_value = FakeResponse({
            "Jobs": {"Job": [job("101")]},
        })

        jobs = brassring.scrape({
            "search_url": "https://jobs.example/search",
            "partner_id": 1,
            "site_id": 2,
        })

        self.assertEqual(len(jobs), 2)
        self.assertEqual(session.return_value.post.call_count, 1)


class TalentBrewTests(unittest.TestCase):
    @patch("scrapers.talentbrew.make_session")
    def test_paginates_json_wrapped_search_results(self, make_session):
        get = make_session.return_value.get
        def payload(job_id, title, page, total=2):
            return FakeResponse({"results": f"""
            <section id="search-results" data-total-job-results="{total}"
                     data-current-page="{page}">
              <ul>
                <li class="sr-job-item">
                  <h3><a class="sr-job-item__link" data-job-id="{job_id}"
                    href="/job/example/{job_id}">{title}</a></h3>
                  <span class="sr-job-location">London, UK</span>
                </li>
              </ul>
            </section>
            """})

        get.side_effect = [
            payload("100", "Markets Analyst", 1),
            payload("101", "Trading Intern", 2),
        ]

        jobs = talentbrew.scrape({
            "base_url": "https://jobs.example.com",
            "page_size": 1,
        })

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], "talentbrew_100")
        self.assertEqual(
            jobs[1]["url"],
            "https://jobs.example.com/job/example/101",
        )
        self.assertEqual(get.call_count, 2)

    @patch("scrapers.talentbrew.make_session")
    def test_supports_custom_card_selectors(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse({"results": """
        <section id="search-results" data-total-job-results="1">
          <li class="custom-item">
            <a class="custom-link" data-job-id="200" href="/job/200">
              <h2 class="custom-title">Portfolio Analyst</h2>
              <span class="custom-location">New York, NY</span>
            </a>
          </li>
        </section>
        """})

        jobs = talentbrew.scrape({
            "base_url": "https://jobs.example.com",
            "item_selector": ".custom-item",
            "link_selector": ".custom-link",
            "title_selector": ".custom-title",
            "location_selector": ".custom-location",
        })

        self.assertEqual(jobs[0]["title"], "Portfolio Analyst")
        self.assertEqual(jobs[0]["location"], "New York, NY")

    @patch("scrapers.talentbrew.make_session")
    def test_passes_structured_facet_filters(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse({"results": """
        <section id="search-results" data-total-job-results="1">
          <li><a data-job-id="300" href="/job/300">
            <h3>Wholesale Banking Graduate</h3>
          </a></li>
        </section>
        """})

        talentbrew.scrape({
            "base_url": "https://jobs.example.com",
            "item_selector": "li",
            "link_selector": "a[data-job-id]",
            "title_selector": "h3",
            "facet_filters": [{
                "ID": 32221824,
                "FacetType": 1,
                "IsApplied": "true",
            }],
        })

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["FacetFilters[0].ID"], 32221824)
        self.assertEqual(params["FacetFilters[0].FacetType"], 1)


class SuccessFactorsAPITests(unittest.TestCase):
    @patch("scrapers.successfactors_api.requests.Session")
    def test_uses_csrf_session_and_paginates_results(self, session):
        session.return_value.get.return_value = FakeResponse(
            text='var CSRFToken = "test-token";'
        )
        session.return_value.post.side_effect = [
            FakeResponse({
                "totalJobs": 11,
                "jobSearchResult": [{"response": {
                    "id": "500",
                    "unifiedStandardTitle": "Markets Analyst",
                    "urlTitle": "Markets-Analyst",
                    "jobLocationShort": ["London, GBR "],
                    "unifiedStandardStart": "07/06/2026",
                }}] * 10,
            }),
            FakeResponse({
                "totalJobs": 11,
                "jobSearchResult": [{"response": {
                    "id": "501",
                    "unifiedStandardTitle": "Trading Intern",
                    "urlTitle": "Trading-Intern",
                    "jobLocationShort": ["Singapore, SGP "],
                }}],
            }),
        ]

        jobs = successfactors_api.scrape({
            "base_url": "https://jobs.example.com",
            "locale": "en_GB",
        })

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["posted"], "2026-06-07")
        self.assertEqual(
            jobs[1]["url"],
            "https://jobs.example.com/job/Trading-Intern/501-en_GB/",
        )
        self.assertEqual(session.return_value.post.call_count, 2)


class TeamtailorTests(unittest.TestCase):
    @patch("scrapers.teamtailor.make_session")
    def test_traverses_turbo_stream_pages(self, make_session):
        get = make_session.return_value.get
        def page(job_id, title, next_page=None):
            next_link = (
                f'<a href="/jobs/show_more?page={next_page}">More</a>'
                if next_page else ""
            )
            return FakeResponse(text=f"""
                <turbo-stream><template><li>
                  <a href="https://jobs.example/jobs/{job_id}-role">
                    {title}
                  </a>
                  <span class="text-base">Markets · London · Hybrid</span>
                </li></template></turbo-stream>
                {next_link}
            """)

        get.side_effect = [
            page("100", "Markets Analyst", 2),
            page("101", "Trading Intern"),
        ]

        jobs = teamtailor.scrape("https://jobs.example")

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], "teamtailor_100")
        self.assertEqual(jobs[1]["title"], "Trading Intern")
        self.assertEqual(get.call_count, 2)


class EmplyTests(unittest.TestCase):
    """Emply get-page API handler (Capital Four)."""

    @patch("scrapers.emply.make_session")
    def test_parses_vacancies_and_builds_ad_urls(self, make_session):
        sess = make_session.return_value
        sess.get.return_value = FakeResponse(
            text="var config = { count: 6, filters: [], langCode: languageKey, "
                 "offset: 0, searchText: '', "
                 "sectionId: '4266b9e7-2a81-403a-80c5-9bf1d57e76fe', };"
        )
        post_resp = FakeResponse(text="")
        post_resp.json = lambda: {"count": 1, "vacancies": [{
            "title": "Analyst Programme", "shortId": "ab12cd",
            "titleAsUrl": "analyst-programme", "location": "Copenhagen",
            "published": "2026-07-01T00:00:00", "directLink": None,
        }]}
        sess.post.return_value = post_resp
        jobs = emply.scrape({"base_url": "https://x.career.emply.com", "tenant": "x"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "emply_x_ab12cd")
        self.assertEqual(jobs[0]["url"],
                         "https://x.career.emply.com/ad/analyst-programme/ab12cd")

    @patch("scrapers.emply.make_session")
    def test_missing_section_id_raises(self, make_session):
        make_session.return_value.get.return_value = FakeResponse(
            text="<html>layout changed, no inline config</html>")
        with self.assertRaises(RuntimeError):
            emply.scrape({"base_url": "https://x.career.emply.com"})

    @patch("scrapers.emply.make_session")
    def test_zero_count_is_trusted_empty(self, make_session):
        sess = make_session.return_value
        sess.get.return_value = FakeResponse(
            text="sectionId: '4266b9e7-2a81-403a-80c5-9bf1d57e76fe'")
        post_resp = FakeResponse(text="")
        post_resp.json = lambda: {"count": 0, "vacancies": []}
        sess.post.return_value = post_resp
        self.assertEqual(emply.scrape({"base_url": "https://x.career.emply.com"}), [])


class ZeroParseGuardTests(unittest.TestCase):
    """Fetched a live page but parsed no jobs → must RAISE, never return [].

    Doctrine: the delister reads [] as "board empty" and purges the firm's
    stored rows, so a zero-parse from a page we DID fetch (moved markup, JS
    shell) has to fail loud instead of silently emptying the board.
    """

    @patch("scrapers.generic.make_session")
    def test_generic_raises_on_empty_html(self, make_session):
        make_session.return_value.get.return_value = FakeResponse(
            text="<html><body>no jobs here</body></html>"
        )
        with self.assertRaises(RuntimeError):
            generic.scrape(
                "https://jobs.example/careers",
                {"container": ".job", "title": ".title"},
            )

    @patch("scrapers.generic.make_session")
    def test_generic_trusted_empty_marker_returns_empty(self, make_session):
        # A target may declare the site's own explicit "no vacancies" prose as
        # `empty_marker` (Arma Partners); zero-parse + marker present => [].
        make_session.return_value.get.return_value = FakeResponse(
            text="<html><body><p>At the moment, we do not have any open "
                 "vacancies. Please check back later.</p></body></html>"
        )
        jobs = generic.scrape(
            "https://jobs.example/careers",
            {"container": ".job", "title": ".title",
             "empty_marker": "we do not have any open vacancies"},
        )
        self.assertEqual(jobs, [])

    @patch("scrapers.teamtailor.make_session")
    def test_teamtailor_raises_on_empty_html(self, make_session):
        # A page with no /jobs/ anchors and no next-page link: HTML loop exits
        # with an empty dict, then the RSS fallback fetches the same shell — not
        # a valid <channel> — so the scraper still fails loud.
        make_session.return_value.get.return_value = FakeResponse(
            text="<html><body>shell, no listings</body></html>"
        )
        with self.assertRaises(RuntimeError):
            teamtailor.scrape("https://jobs.example")


class TrustedEmptyTests(unittest.TestCase):
    """Fetched a live page that AUTHORITATIVELY reports zero openings → [].

    Complement to ZeroParseGuardTests: a trustworthy empty signal (a valid but
    itemless RSS channel, or an explicit "no jobs" empty-state) must return []
    so the delister can retire stale rows, without a broken selector ever being
    mistaken for an empty board.
    """

    @patch("scrapers.pinpoint.make_session")
    def test_pinpoint_valid_empty_channel_returns_empty(self, make_session):
        make_session.return_value.get.return_value = FakeResponse(
            content=b"""<?xml version="1.0"?>
            <rss version="2.0"><channel>
              <title>Careers</title><link>https://x.pinpointhq.com/jobs</link>
            </channel></rss>"""
        )
        self.assertEqual(
            pinpoint.scrape("https://x.pinpointhq.com/jobs.rss"), []
        )

    @patch("scrapers.pinpoint.make_session")
    def test_pinpoint_missing_channel_raises(self, make_session):
        # A parseable XML document that is NOT an RSS feed (no <channel>) is a
        # shell/error page → fail loud, not trusted-empty.
        make_session.return_value.get.return_value = FakeResponse(
            content=b"<html><body>error</body></html>"
        )
        with self.assertRaises(RuntimeError):
            pinpoint.scrape("https://x.pinpointhq.com/jobs.rss")

    @patch("scrapers.teamtailor.make_session")
    def test_teamtailor_falls_back_to_empty_rss(self, make_session):
        # HTML shell (0 anchors) then a valid but itemless RSS channel → [].
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse(text="<html><body>client-side shell</body></html>"),
            FakeResponse(content=b"""<?xml version="1.0"?>
            <rss version="2.0"><channel>
              <title>Careers</title><link>https://x.teamtailor.com/jobs</link>
            </channel></rss>"""),
        ]
        self.assertEqual(teamtailor.scrape("https://x.teamtailor.com"), [])

    @patch("scrapers.teamtailor.make_session")
    def test_teamtailor_recovers_jobs_from_rss_when_html_stale(
        self, make_session
    ):
        # HTML shell (0 anchors) but the RSS carries openings → parse them.
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse(text="<html><body>client-side shell</body></html>"),
            FakeResponse(content=b"""<?xml version="1.0"?>
            <rss version="2.0"><channel>
              <title>Careers</title>
              <item>
                <title>Markets Analyst</title>
                <link>https://x.teamtailor.com/jobs/900-markets-analyst</link>
              </item>
            </channel></rss>"""),
        ]
        jobs = teamtailor.scrape("https://x.teamtailor.com")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "teamtailor_900")
        self.assertEqual(jobs[0]["title"], "Markets Analyst")

    @patch("scrapers.peoplebank.make_session")
    def test_peoplebank_empty_state_marker_returns_empty(self, make_session):
        make_session.return_value.get.return_value = FakeResponse(text="""
          <div id="results">
            <p>There are currently no jobs matching your search criteria.</p>
          </div>
        """)
        self.assertEqual(
            peoplebank.scrape("https://jobs.example/jobs/category/144"), []
        )

    @patch("scrapers.peoplebank.make_session")
    def test_peoplebank_no_rows_no_marker_raises(self, make_session):
        # #results rendered, no rows AND no empty-state string → moved markup.
        make_session.return_value.get.return_value = FakeResponse(text="""
          <div id="results"><div class="cruft">unexpected layout</div></div>
        """)
        with self.assertRaises(RuntimeError):
            peoplebank.scrape("https://jobs.example/jobs/category/144")


class SuccessFactorsTests(unittest.TestCase):
    @patch("scrapers.successfactors.make_session")
    def test_advances_by_actual_page_size(self, session):
        first = """
          <span class="paginationLabel">Results 1 - 2 of 3</span>
          <table><tr><td>
            <a class="jobTitle-link" href="/job/A/100/">Analyst</a>
            <span class="jobLocation">Berlin</span>
          </td></tr><tr><td>
            <a class="jobTitle-link" href="/job/B/101/">Intern</a>
            <span class="jobLocation">Paris</span>
          </td></tr></table>
        """
        second = """
          <span class="paginationLabel">Results 3 - 3 of 3</span>
          <table><tr><td>
            <a class="jobTitle-link" href="/job/C/102/">Trader</a>
            <span class="jobLocation">London</span>
          </td></tr></table>
        """
        session.return_value.get.side_effect = [
            FakeResponse(text=first),
            FakeResponse(text=second),
        ]

        jobs = successfactors.scrape("https://jobs.example")

        self.assertEqual(len(jobs), 3)
        self.assertEqual(
            session.return_value.get.call_args_list[1].kwargs["params"]["startrow"],
            2,
        )

    @patch("scrapers.successfactors.make_session")
    def test_paginates_localized_german_total(self, session):
        def page(job_id, start, end):
            return FakeResponse(text=f"""
              <div>Es werden {start} bis {end} von 2 Stellen angezeigt</div>
              <table><tr><td>
                <a class="jobTitle-link" href="/job/A/{job_id}/">Analyst</a>
              </td></tr></table>
            """)

        session.return_value.get.side_effect = [
            page("100", 1, 1),
            page("101", 2, 2),
        ]

        jobs = successfactors.scrape("https://jobs.example")

        self.assertEqual(len(jobs), 2)
        self.assertEqual(session.return_value.get.call_count, 2)

    @patch("scrapers.successfactors.make_session")
    def test_supports_spanish_total_filters_and_tile_duplicates(self, session):
        session.return_value.get.return_value = FakeResponse(text="""
          <span id="tile-search-results-label">Showing 1 Job</span>
          <ul><li class="job-tile">
            <a class="jobTitle-link" href="/job/Madrid/200/">CIB Analyst</a>
            <a class="jobTitle-link" href="/job/Madrid/200/">CIB Analyst</a>
          </li></ul>
          <div>Resultados 1 - 1 de 1</div>
        """)

        jobs = successfactors.scrape(
            "https://jobs.example",
            search_params={"department": "Corporate Banking"},
        )

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            session.return_value.get.call_args.kwargs["params"]["department"],
            "Corporate Banking",
        )


class TalentsoftTests(unittest.TestCase):
    @patch("scrapers.talentsoft.make_session")
    def test_paginates_and_validates_reported_total(self, session):
        def page(job_id, title, city):
            return FakeResponse(text=f"""
              <span id="x_Pagination_TotalOffers">2 vacancy(s)</span>
              <article class="ts-offer-card">
                <a class="ts-offer-card__title-link"
                   href="/job/job-{job_id}_{job_id}.aspx">{title}</a>
                <ul class="ts-offer-card-content__list">
                  <li>Permanent</li><li>France</li><li>{city}</li>
                </ul>
              </article>
            """)

        session.return_value.get.side_effect = [
            page("100", "Markets Analyst", "Paris"),
            page("101", "Trading Intern", "Montrouge"),
        ]

        jobs = talentsoft.scrape({
            "board_url": "https://jobs.example/job/list-of-jobs.aspx",
            "locale": 2057,
        })

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["location"], "Paris, France")
        self.assertEqual(session.return_value.get.call_count, 2)


class InterviewebTests(unittest.TestCase):
    @patch("scrapers.intervieweb.make_session")
    def test_maps_complete_json_feed(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse(payload=[{
            "id": 10,
            "title": "Investment Banking Analyst",
            "url": "https://jobs.example/10",
            "location": "Milan",
            "published": "21-05-2026 (15:36)",
        }])

        jobs = intervieweb.scrape("https://jobs.example/feed")

        self.assertEqual(jobs[0]["id"], "intervieweb_10")
        self.assertEqual(jobs[0]["posted"], "2026-05-21")


class PeopleBankTests(unittest.TestCase):
    @patch("scrapers.peoplebank.make_session")
    def test_excludes_featured_jobs_from_category_results(self, session):
        session.return_value.get.return_value = FakeResponse(text="""
          <div id="results">
            <div class="featured"><ul class="jobs"><li>
              <a class="in-app" itemprop="url" href="/jobs/job/Unrelated/10">
                <span class="job-list-title">Unrelated Featured Job</span>
              </a>
            </li></ul></div>
            <ul class="jobs"><li>
              <a class="in-app" itemprop="url" href="/jobs/job/Markets/11">
                <span class="job-list-title">Capital Markets Analyst</span>
                <span itemprop="address">London, United Kingdom</span>
                <span itemprop="datePosted">1 June, 2026</span>
              </a>
            </li></ul>
          </div>
        """)

        jobs = peoplebank.scrape("https://jobs.example/jobs/category/144")

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "peoplebank_11")


class PhenomTests(unittest.TestCase):
    @patch("scrapers.phenom._page")
    def test_paginates_and_applies_reported_company_facet(self, page):
        page.side_effect = [
            {
                "status": 200,
                "totalHits": 2,
                "data": {
                    "jobs": [{
                        "reqId": "10",
                        "title": "Portfolio Analyst",
                        "unit": "Asset Manager",
                        "location": "Frankfurt",
                        "postedDate": "2026-06-01T00:00:00Z",
                    }],
                    "aggregations": [{
                        "field": "unit",
                        "value": {"Asset Manager": 1},
                    }],
                },
            },
            {
                "status": 200,
                "totalHits": 2,
                "data": {
                    "jobs": [{
                        "reqId": "11",
                        "title": "Insurance Specialist",
                        "unit": "Insurer",
                    }],
                },
            },
        ]

        jobs = phenom.scrape({
            "search_url": "https://jobs.example/search-results",
            "base_url": "https://jobs.example",
            "source_id": "asset",
            "filter_field": "unit",
            "filter_value": "Asset Manager",
            "workers": 1,
        })

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "phenom_asset_10")
        self.assertEqual(
            jobs[0]["url"],
            "https://jobs.example/job/10/portfolio-analyst",
        )


class SocieteGeneraleTests(unittest.TestCase):
    @patch("scrapers.societegenerale.make_session")
    def test_validates_server_rendered_total(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse(text="""
          <div class="views-element-container">
            <strong>1</strong>
            <div data-offer-id="260001AB">
              <a href="https://careers.example/job-offers/analyst-260001AB-en">
                Markets Analyst
              </a>
              <div class="tags"><span class="nobreak">Paris, France</span></div>
            </div>
          </div>
        """)

        jobs = societegenerale.scrape()

        self.assertEqual(jobs[0]["id"], "socgen_260001AB")
        self.assertEqual(jobs[0]["location"], "Paris, France")


class JaneStreetTests(unittest.TestCase):
    @patch("scrapers.janestreet.make_session")
    def test_maps_public_json_feed(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse([
            {"id": 123, "position": "Graduate Trader", "city": "LDN"},
        ])

        self.assertEqual(janestreet.scrape(), [{
            "id": "js_123",
            "title": "Graduate Trader",
            "url": "https://www.janestreet.com/join-jane-street/position/123/",
            "location": "London",
            "posted": "",
        }])


class BeesiteTests(unittest.TestCase):
    @patch("scrapers.beesite.make_session")
    def test_supports_custom_host_channel_and_id_field(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse({
            "SearchResult": {
                "SearchResultCountAll": 1,
                "SearchResultItems": [{
                    "MatchedObjectDescriptor": {
                        "ID": "300",
                        "PositionTitle": "Graduate Portfolio Analyst",
                        "PositionURI": "https://jobs.example/300",
                        "PositionLocation": [{
                            "CityName": "Frankfurt",
                            "CountryName": "Germany",
                        }],
                        "PublicationStartDate": "2026-06-01",
                    },
                }],
            },
        })

        jobs = beesite.scrape({
            "base_url": "https://jobapi.example/search/",
            "language": "DE",
            "search_criteria": [{
                "CriterionName": "PublicationChannel.Code",
                "CriterionValue": ["12"],
            }],
        })

        self.assertEqual(jobs[0]["id"], "beesite_custom_300")
        self.assertEqual(jobs[0]["location"], "Frankfurt, Germany")
        payload = get.call_args.kwargs["params"]["data"]
        self.assertIn('"LanguageCode": "DE"', payload)


class AvatureTests(unittest.TestCase):
    @patch("scrapers.avature.make_session")
    def test_accepts_path_based_job_ids_and_results_count(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse(text="""
            <div>1 results</div>
            <article class="article article--result">
              <h3><a href="https://jobs.example/careers/JobDetail/Analyst/400">
                Markets Analyst
              </a></h3>
            </article>
        """)

        jobs = avature.scrape("https://jobs.example/careers/SearchJobs/")

        self.assertEqual(jobs[0]["id"], "avature_400")
        self.assertEqual(jobs[0]["title"], "Markets Analyst")

    @patch("scrapers.avature.make_session")
    def test_ignores_social_links_and_uses_actual_page_size(self, make_session):
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse(text="""
                <div>2 results</div>
                <article class="article article--result">
                  <a href="https://facebook.example/share?JobDetail=500">
                    Facebook
                  </a>
                  <h3><a href="https://jobs.example/careers/JobDetail/Analyst/500">
                    Markets Analyst
                  </a></h3>
                  <span class="job-info-icon_world">Milan, Italy</span>
                </article>
            """),
            FakeResponse(text="""
                <article class="article article--result">
                  <h3><a href="https://jobs.example/careers/JobDetail/Intern/501">
                    Trading Intern
                  </a></h3>
                </article>
            """),
        ]

        jobs = avature.scrape(
            "https://jobs.example/careers/SearchJobs/",
            page_size=100,
        )

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["location"], "Milan, Italy")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["jobOffset"], 1)

    @patch("scrapers.avature.make_session")
    def test_posted_date_is_not_mistaken_for_location(self, make_session):
        # Baloise-style card: no location anywhere, only a posted date in the
        # subtitle slot the location fallback reads. Rows were stored with
        # location='Posted 19-Jun-2026' — a date must never become a location.
        get = make_session.return_value.get
        get.return_value = FakeResponse(text="""
            <div>1 results</div>
            <article class="article article--result">
              <h3><a href="https://jobs.example/careers/JobDetail?jobId=600">
                Junior Portfolio Manager
              </a></h3>
              <div class="article__header__text__subtitle">
                <span class="list-item-posted">Posted 10-Jul-2026</span>
              </div>
            </article>
        """)

        jobs = avature.scrape("https://jobs.example/careers/SearchJobs/")

        self.assertEqual(jobs[0]["location"], "")

    @patch("scrapers.avature.make_session")
    def test_subtitle_location_survives_next_to_posted_date(self, make_session):
        # Subtitle carrying BOTH a real location and a posted-date span keeps
        # the location once the date span is stripped.
        get = make_session.return_value.get
        get.return_value = FakeResponse(text="""
            <div>1 results</div>
            <article class="article article--result">
              <h3><a href="https://jobs.example/careers/JobDetail?jobId=601">
                Junior Trader
              </a></h3>
              <div class="article__header__text__subtitle">
                Basel, Switzerland
                <span class="list-item-posted">Posted 10-Jul-2026</span>
              </div>
            </article>
        """)

        jobs = avature.scrape("https://jobs.example/careers/SearchJobs/")

        self.assertEqual(jobs[0]["location"], "Basel, Switzerland")

    @patch("scrapers.avature.make_session")
    def test_accepts_job_detail_urls_without_a_slug(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse(text="""
            <div>1 results</div>
            <article class="article article--result">
              <h3><a href="https://jobs.example/careers/JobDetail/91982">
                Markets Intern
              </a></h3>
              <span class="job-info-icon_world">Vienna, Austria</span>
            </article>
        """)

        jobs = avature.scrape("https://jobs.example/careers/SearchJobs/")

        self.assertEqual(jobs[0]["id"], "avature_91982")
        self.assertEqual(jobs[0]["title"], "Markets Intern")

    @patch("scrapers.avature.make_session")
    def test_accepts_query_parameter_job_ids(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse(text="""
            <div>1 results</div>
            <article class="article article--result">
              <h3><a href="https://jobs.example/JobDetail?jobId=22579">
                Infrastructure Debt Investment
              </a></h3>
            </article>
        """)

        jobs = avature.scrape("https://jobs.example/careers/SearchJobs/")

        self.assertEqual(jobs[0]["id"], "avature_22579")

    @patch("scrapers.avature.make_session")
    def test_follows_explicit_pagination_without_a_total_label(self, make_session):
        get = make_session.return_value.get
        def page(job_id, include_pages=False):
            pages = """
              <a href="/OpenRoles/?jobRecordsPerPage=1&jobOffset=1">2</a>
              <a href="/OpenRoles/?jobRecordsPerPage=1&jobOffset=2">3</a>
            """ if include_pages else ""
            return FakeResponse(text=f"""
              {pages}
              <article class="article--result">
                <a href="https://jobs.example/JobDetail/Role/{job_id}">
                  Role {job_id}
                </a>
              </article>
            """)

        get.side_effect = [
            page("100", True),
            page("101"),
            page("102"),
        ]

        jobs = avature.scrape("https://jobs.example/OpenRoles/", page_size=1)

        self.assertEqual(len(jobs), 3)
        self.assertEqual(get.call_count, 3)


class AttraxTests(unittest.TestCase):
    @patch("scrapers.attrax.make_session")
    def test_traverses_full_inventory_then_filters_business_unit(self, session):
        def page(job_id, unit):
            return FakeResponse(text=f"""
              <span class="attrax-pagination__total-results">2 results</span>
              <div class="attrax-vacancy-tile" data-jobid="{job_id}">
                <a class="attrax-vacancy-tile__title" href="/job/{job_id}">
                  Role {job_id}
                </a>
                <div class="attrax-vacancy-tile__option-business-unit-valueset">
                  {unit}
                </div>
                <div class="attrax-vacancy-tile__location-freetext">
                  <span class="attrax-vacancy-tile__item-value">London</span>
                </div>
              </div>
            """)

        session.return_value.get.side_effect = [
            page("10", "Legal & General Investment Management"),
            page("11", "Legal & General Retail"),
        ]

        jobs = attrax.scrape({
            "search_url": "https://jobs.example/jobs",
            "business_unit": "Legal & General Investment Management",
        })

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "attrax_10")
        self.assertEqual(session.return_value.get.call_count, 2)


class RadancyTests(unittest.TestCase):
    @patch("scrapers.radancy.make_session")
    def test_validates_filtered_server_rendered_total(self, session):
        session.return_value.get.return_value = FakeResponse(text="""
          <section id="search-results" data-total-job-results="1"
                   data-records-per-page="15">
            <li>
              <a class="search-results-list__job-link" data-job-id="20"
                 href="/en/job/20">Investment Intern</a>
              <span class="job-list-01-list__job-info--location">
                <span>Munich, Germany</span>
              </span>
            </li>
          </section>
        """)

        jobs = radancy.scrape("https://jobs.example/search?facet=MEAG")

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["location"], "Munich, Germany")


class BundesbankTests(unittest.TestCase):
    @patch("scrapers.bundesbank.make_session")
    def test_follows_all_result_pages(self, make_session):
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse(text="""
                <a href="/action/de/729936/bbksearch?pageNumString=1">2</a>
                <li class="resultlist__item">
                  <div class="teasable__title"><div class="h2">Markets Trainee</div></div>
                  <div class="teasable__info">Deadline | Frankfurt am Main</div>
                  <a class="teasable__link" href="/de/job--100"></a>
                </li>
            """),
            FakeResponse(text="""
                <li class="resultlist__item">
                  <div class="teasable__title"><div class="h2">Risk Analyst</div></div>
                  <div class="teasable__info">Deadline | Berlin</div>
                  <a class="teasable__link" href="/de/job-101"></a>
                </li>
            """),
        ]

        jobs = bundesbank.scrape()

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[1]["location"], "Berlin")


class DeutscheBoerseTests(unittest.TestCase):
    @patch("scrapers.deutscheboerse.make_session")
    def test_maps_all_sitemap_job_postings(self, make_session):
        get = make_session.return_value.get
        sitemap = b"""<?xml version="1.0"?>
          <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://careers.example/offer/markets-intern/abc</loc></url>
          </urlset>
        """
        posting = """
          <script type="application/ld+json">
            {
              "@type": "JobPosting",
              "title": "Markets Intern",
              "identifier": {"value": "abc"},
              "url": "https://careers.example/offer/markets-intern/abc",
              "datePosted": "2026-06-07",
              "jobLocation": [{
                "address": {
                  "addressLocality": "Frankfurt",
                  "addressCountry": "DE"
                }
              }]
            }
          </script>
        """
        get.side_effect = lambda url, **kwargs: (
            FakeResponse(content=sitemap)
            if url.endswith("sitemap.xml")
            else FakeResponse(text=posting)
        )

        jobs = deutscheboerse.scrape()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "deutscheboerse_abc")
        self.assertEqual(jobs[0]["location"], "Frankfurt, DE")

    @patch("scrapers.deutscheboerse.make_session")
    def test_ignores_withdrawn_sitemap_offer_returning_404(self, make_session):
        get = make_session.return_value.get
        sitemap = b"""<?xml version="1.0"?>
          <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://careers.example/offer/markets-intern/abc</loc></url>
            <url><loc>https://careers.example/offer/withdrawn-role/def</loc></url>
          </urlset>
        """
        posting = """
          <script type="application/ld+json">
            {
              "@type": "JobPosting",
              "title": "Markets Intern",
              "identifier": {"value": "abc"},
              "url": "https://careers.example/offer/markets-intern/abc",
              "jobLocation": []
            }
          </script>
        """

        def response(url, **kwargs):
            if url.endswith("sitemap.xml"):
                return FakeResponse(content=sitemap)
            if url.endswith("/def"):
                return FakeResponse(status_code=404)
            return FakeResponse(text=posting)

        get.side_effect = response

        jobs = deutscheboerse.scrape()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "deutscheboerse_abc")


class EuronextTests(unittest.TestCase):
    @patch("scrapers.euronext.make_session")
    def test_follows_drupal_pages_and_maps_locations(self, make_session):
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse(text="""
                <ul class="pagination"><li><a href="?page=1">2</a></li></ul>
                <table class="views-table"><tbody><tr>
                  <td class="views-field-field-country">France</td>
                  <td class="views-field-field-job-title">
                    <a href="/en/about/careers/job-offers/r100-markets-intern">
                      Markets Intern
                    </a>
                  </td>
                  <td class="views-field-name">Paris</td>
                </tr></tbody></table>
            """),
            FakeResponse(text="""
                <table class="views-table"><tbody><tr>
                  <td class="views-field-field-country">Netherlands</td>
                  <td class="views-field-field-job-title">
                    <a href="/en/about/careers/job-offers/r101-risk-analyst">
                      Risk Analyst
                    </a>
                  </td>
                  <td class="views-field-name">Amsterdam</td>
                </tr></tbody></table>
            """),
        ]

        jobs = euronext.scrape()

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[1]["location"], "Amsterdam, Netherlands")


class RWETests(unittest.TestCase):
    @patch("scrapers.rwe.make_session")
    def test_filters_company_and_checks_complete_pagination(self, make_session):
        post = make_session.return_value.post
        post.side_effect = [
            FakeResponse({
                "TotalCount": 2,
                "Results": [{
                    "Id": "100-en_GB",
                    "Title": "Trading Analyst",
                    "Url": "https://jobs.rwe.com/job/100",
                    "Location": "Essen, DE",
                    "Created": "2026-06-07T00:00:00",
                    "CustomField1": "RWE Supply & Trading",
                }],
            }),
            FakeResponse({
                "TotalCount": 2,
                "Results": [{
                    "Id": "101-en_GB",
                    "Title": "Commercial Intern",
                    "Url": "https://jobs.rwe.com/job/101",
                    "Location": "London, GB",
                    "Created": "2026-06-06T00:00:00",
                    "CustomField1": "RWE Supply & Trading",
                }],
            }),
        ]

        jobs = rwe.scrape({"company": "RWE Supply & Trading"})

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["id"], "rwe_100-en_GB")
        self.assertEqual(jobs[1]["posted"], "2026-06-06")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["skip"], 1)


class TalnetTests(unittest.TestCase):
    @patch("scrapers.talnet.make_session")
    def test_maps_complete_board_and_detail_location(self, session):
        session.return_value.get.side_effect = [
            FakeResponse(text="""
                <div class="results_meta"><h2>1 result matches!</h2></div>
                <table class="solr_search_list">
                  <tr class="details_row">
                    <td><a class="subject" href="https://jobs.example/opp/42-intern/en">
                      Markets Intern
                    </a></td>
                  </tr>
                </table>
            """),
            FakeResponse(text="""
                <p><strong><span>Location:</span></strong>&nbsp; London</p>
            """),
        ]

        jobs = talnet.scrape("https://jobs.example/board")

        self.assertEqual(jobs[0]["id"], "talnet_42")
        self.assertEqual(jobs[0]["location"], "London")


class WellsFargoTests(unittest.TestCase):
    @patch("scrapers.wellsfargo.make_session")
    def test_maps_english_sitemap_jobs_and_deduplicates_ids(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse(content=b"""<?xml version="1.0"?>
          <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url>
              <loc>https://www.wellsfargojobs.com/en/jobs/r-123/markets-analyst/</loc>
              <lastmod>2026-06-07T00:00:00Z</lastmod>
            </url>
            <url>
              <loc>https://www.wellsfargojobs.com/en/jobs/r-123/markets-analyst/</loc>
              <lastmod>2026-06-07T00:00:00Z</lastmod>
            </url>
            <url>
              <loc>https://www.wellsfargojobs.com/fr/jobs/r-123/analyste-marches/</loc>
            </url>
          </urlset>
        """)

        jobs = wellsfargo.scrape()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "wellsfargo_123")
        self.assertEqual(jobs[0]["title"], "Markets Analyst")


class BreezyTests(unittest.TestCase):
    @patch("scrapers.breezy.make_session")
    def test_maps_public_json_board(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse([{
            "id": "abc",
            "name": "Graduate Energy Trader",
            "url": "https://example.breezy.hr/p/abc",
            "published_date": "2026-06-02T12:00:00Z",
            "location": {"name": "London, GB"},
        }])

        jobs = breezy.scrape("example")

        self.assertEqual(jobs[0]["id"], "breezy_example_abc")
        self.assertEqual(jobs[0]["location"], "London, GB")


class DirectEmployersTests(unittest.TestCase):
    @patch("scrapers.directemployers.make_session")
    def test_paginates_public_search_api(self, make_session):
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse({
                "jobs": [{
                    "guid": "ABC",
                    "title_exact": "Junior Fixed Income Sales",
                    "title_slug": "junior-fixed-income-sales",
                    "location_exact": "New York, NY",
                    "date_new": "2026-06-01T10:00:00Z",
                }],
                "pagination": {"has_more_pages": True},
            }),
            FakeResponse({
                "jobs": [{
                    "guid": "DEF",
                    "title_exact": "Trading Analyst",
                    "title_slug": "trading-analyst",
                    "location_exact": "London, GBR",
                }],
                "pagination": {"has_more_pages": False},
            }),
        ]

        jobs = directemployers.scrape({"host": "example.dejobs.org"})

        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            jobs[0]["url"],
            "https://example.dejobs.org/new-york-ny/"
            "junior-fixed-income-sales/ABC/job/",
        )
        self.assertEqual(get.call_count, 2)


class DEShawTests(unittest.TestCase):
    @patch("scrapers.deshaw.make_session")
    def test_extracts_unique_job_links(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse(text="""
            <a href="/careers/quantitative-analyst-1234">
              icon Quantitative Analyst: Description text
            </a>
            <a href="/careers/quantitative-analyst-1234">Duplicate</a>
            <a href="/careers/internships">Internships</a>
        """)

        jobs = deshaw.scrape()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "deshaw_1234")
        self.assertEqual(jobs[0]["title"], "Quantitative Analyst")


class JibeTests(unittest.TestCase):
    @patch("scrapers.jibe.make_session")
    def test_paginates_public_jobs_api(self, make_session):
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse({
                "totalCount": 2,
                "jobs": [{"data": {
                    "req_id": "10",
                    "title": "Graduate Trader",
                    "full_location": "Dublin",
                    "posted_date": "2026-06-01T10:00:00+0000",
                }}],
            }),
            FakeResponse({
                "totalCount": 2,
                "jobs": [{"data": {
                    "req_id": "11",
                    "title": "Quant Intern",
                    "short_location": "London",
                }}],
            }),
        ]

        jobs = jibe.scrape({
            "base_url": "https://careers.example.com",
            "company_id": "example",
        })

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["url"], "https://careers.example.com/jobs/10")
        self.assertEqual(jobs[0]["posted"], "2026-06-01")

    @patch("scrapers.jibe.make_session")
    def test_supports_custom_page_size_and_domain(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse({"totalCount": 0, "jobs": []})

        jibe.scrape({
            "base_url": "https://careers.example.com",
            "company_id": "example",
            "page_size": 100,
            "domain": "example.jibeapply.com",
        })

        self.assertEqual(get.call_args.kwargs["params"]["limit"], 100)
        self.assertEqual(
            get.call_args.kwargs["params"]["domain"],
            "example.jibeapply.com",
        )


class HiBobTests(unittest.TestCase):
    @patch("scrapers.hibob.make_session")
    def test_maps_public_job_ads(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse({
            "jobAdDetails": [{
                "id": "abc-123",
                "title": "Junior Quantitative Developer",
                "site": "London",
                "country": "United Kingdom",
                "publishedAt": "2026-05-08T10:00:00Z",
            }],
        })

        jobs = hibob.scrape({
            "base_url": "https://example.careers.hibob.com",
            "company_identifier": "example",
        })

        self.assertEqual(jobs[0]["id"], "hibob_abc-123")
        self.assertEqual(jobs[0]["location"], "London, United Kingdom")
        self.assertEqual(jobs[0]["posted"], "2026-05-08")


class PinpointTests(unittest.TestCase):
    @patch("scrapers.pinpoint.make_session")
    def test_maps_rss_items(self, make_session):
        get = make_session.return_value.get
        get.return_value = FakeResponse(content=b"""<?xml version="1.0"?>
        <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel><item>
            <title>Junior Analyst</title>
            <link>https://example.pinpointhq.com/jobs/123</link>
            <pubDate>Wed, 22 Apr 2026 15:33:05 +0100</pubDate>
            <content:encoded><![CDATA[
              <p><strong>Location: </strong>London</p>
            ]]></content:encoded>
          </item></channel>
        </rss>""")

        jobs = pinpoint.scrape("https://example.pinpointhq.com/jobs.rss")

        self.assertEqual(jobs[0]["id"], "pinpoint_123")
        self.assertEqual(jobs[0]["location"], "London")
        self.assertEqual(jobs[0]["posted"], "2026-04-22")


class WorkableTests(unittest.TestCase):
    @patch("scrapers.workable.make_session")
    def test_maps_public_jobs_api(self, make_session):
        post = make_session.return_value.post
        post.return_value = FakeResponse({
            "results": [{
                "shortcode": "ABC123",
                "title": "Graduate Trader",
                "location": {"city": "Sydney", "country": "Australia"},
                "created_at": "2026-06-01T10:00:00Z",
            }],
        })

        jobs = workable.scrape("example")

        self.assertEqual(jobs[0]["id"], "workable_ABC123")
        self.assertEqual(jobs[0]["location"], "Sydney, Australia")
        self.assertEqual(jobs[0]["url"], "https://apply.workable.com/example/j/ABC123/")


class EightfoldTests(unittest.TestCase):
    @patch("scrapers.eightfold.make_session")
    def test_paginates_all_positions(self, make_session):
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse({
                "count": 11,
                "positions": [{
                    "id": 100,
                    "posting_name": "Graduate Trader",
                    "location": "London",
                    "t_create": 1780272000,
                    "canonicalPositionUrl": "https://jobs.example/careers/job/100",
                }],
            }),
            FakeResponse({
                "count": 11,
                "positions": [{
                    "id": 101,
                    "name": "Quant Intern",
                    "location": "Paris",
                }],
            }),
        ]

        jobs = eightfold.scrape({
            "base_url": "https://jobs.example",
            "domain": "example.com",
        })

        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["posted"], "2026-06-01")
        self.assertEqual(jobs[1]["url"], "https://jobs.example/careers/job/101")
        self.assertEqual(get.call_count, 2)


class OracleHCMTests(unittest.TestCase):
    @patch("scrapers.oracle_hcm.make_session")
    def test_paginates_requisitions_using_reported_total(self, make_session):
        get = make_session.return_value.get
        get.side_effect = [
            FakeResponse({"items": [{
                "TotalJobsCount": 2,
                "requisitionList": [{
                    "Id": "200",
                    "Title": "Markets Analyst",
                    "PrimaryLocation": "Frankfurt",
                    "PostedDate": "2026-06-01",
                }],
            }]}),
            FakeResponse({"items": [{
                "TotalJobsCount": 2,
                "requisitionList": [{
                    "Id": "201",
                    "Title": "Trading Intern",
                    "PrimaryLocation": "London",
                    "PostedDate": "2026-06-02",
                }],
            }]}),
        ]

        jobs = oracle_hcm.scrape({
            "base_url": "https://example.oraclecloud.com",
            "site": "CX_1",
        })

        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            jobs[0]["url"],
            "https://example.oraclecloud.com/hcmUI/"
            "CandidateExperience/en/sites/CX_1/job/200",
        )
        self.assertEqual(get.call_count, 2)


class BeesiteCompletenessTests(unittest.TestCase):
    """Fail-loud guards added to beesite.py."""

    @patch("scrapers.beesite.make_session")
    def test_raises_when_page1_empty_but_total_positive(self, make_session):
        make_session.return_value.get.return_value = FakeResponse({
            "SearchResult": {
                "SearchResultCountAll": 500,
                "SearchResultItems": [],
            },
        })

        with self.assertRaises(RuntimeError) as ctx:
            beesite.scrape({"base_url": "https://jobs.example/", "tenant": "db"})
        self.assertIn("500", str(ctx.exception))

    @patch("scrapers.beesite.make_session")
    def test_raises_on_severe_shortfall(self, make_session):
        # Reports 100 but only returns 1 item → well below 90 % band.
        make_session.return_value.get.return_value = FakeResponse({
            "SearchResult": {
                "SearchResultCountAll": 100,
                "SearchResultItems": [{
                    "MatchedObjectDescriptor": {
                        "PositionID": "1",
                        "PositionTitle": "Analyst",
                        "PositionURI": "https://jobs.example/1",
                        "PositionLocation": [],
                        "PublicationStartDate": "2026-01-01",
                    },
                }],
            },
        })

        with self.assertRaises(RuntimeError) as ctx:
            beesite.scrape({"base_url": "https://jobs.example/", "tenant": "db"})
        self.assertIn("refusing partial result", str(ctx.exception))


class OracleHCMCompletenessTests(unittest.TestCase):
    """Fail-loud guards added to oracle_hcm.py."""

    @patch("scrapers.oracle_hcm.make_session")
    def test_raises_when_page1_returns_no_requisitions_but_total_positive(
        self, make_session
    ):
        make_session.return_value.get.return_value = FakeResponse({"items": [{
            "TotalJobsCount": 7000,
            "requisitionList": [],
        }]})

        with self.assertRaises(RuntimeError) as ctx:
            oracle_hcm.scrape({
                "base_url": "https://example.oraclecloud.com",
                "site": "CX_1",
            })
        self.assertIn("7000", str(ctx.exception))

    @patch("scrapers.oracle_hcm.make_session")
    def test_raises_on_severe_shortfall(self, make_session):
        # Page 1 has 1 job but total is 1000.
        make_session.return_value.get.return_value = FakeResponse({"items": [{
            "TotalJobsCount": 1000,
            "requisitionList": [{
                "Id": "1",
                "Title": "Analyst",
                "PrimaryLocation": "London",
                "PostedDate": "2026-01-01",
            }],
        }]})

        with self.assertRaises(RuntimeError) as ctx:
            oracle_hcm.scrape({
                "base_url": "https://example.oraclecloud.com",
                "site": "CX_1",
            })
        self.assertIn("refusing partial result", str(ctx.exception))


class JibeCompletenessTests(unittest.TestCase):
    """Fail-loud guards added to jibe.py."""

    @patch("scrapers.jibe.make_session")
    def test_raises_when_totalcount_missing_from_response(self, make_session):
        make_session.return_value.get.return_value = FakeResponse({
            "jobs": [{"data": {
                "req_id": "10",
                "title": "Analyst",
            }}],
            # totalCount deliberately absent
        })

        with self.assertRaises(RuntimeError) as ctx:
            jibe.scrape({"base_url": "https://careers.example.com"})
        self.assertIn("schema drift", str(ctx.exception))


class DirectEmployersCompletenessTests(unittest.TestCase):
    """Fail-loud guards added to directemployers.py."""

    @patch("scrapers.directemployers.make_session")
    def test_raises_on_max_pages_exhaustion(self, make_session):
        import scrapers.directemployers as de
        orig_max = de.MAX_PAGES
        de.MAX_PAGES = 2
        try:
            make_session.return_value.get.return_value = FakeResponse({
                "jobs": [{"guid": "A", "title_exact": "Analyst",
                          "title_slug": "analyst", "location_exact": "NYC"}],
                "pagination": {"has_more_pages": True},
            })
            with self.assertRaises(RuntimeError) as ctx:
                de.scrape({"host": "example.dejobs.org"})
            self.assertIn("MAX_PAGES", str(ctx.exception))
        finally:
            de.MAX_PAGES = orig_max


if __name__ == "__main__":
    unittest.main()
