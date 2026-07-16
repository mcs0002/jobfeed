"""
All-Haiku structured tagging for every stored role.

This pass assigns *homogeneous facet labels* the web UI filters on — area/desk,
seniority, type, normalized location, work mode. It runs on title + company +
raw location + a short description excerpt, so it's cheap (~$1-2/mo at high
volume; Haiku 4.5 = $1/$5 per 1M in/out) and fast.

Doing it all with Haiku — rather than a keyword classifier with an LLM fallback
— keeps the labels consistent across the whole set: every role is tagged from
the same vocabulary by the same judge, so the UI dropdowns stay clean.

Mechanics: `claude` CLI subprocess (shared auth, no separate API key), jobs
batched (BATCH_SIZE=20), one pipe-delimited line per job, with sub-batch/single
retry on failure. On total failure a job keeps blank tags (still stored,
filterable as "unclassified") — tagging must never break a scan.
"""
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone

from claude_cli import _claude_bin, CLAUDE_BIN_CANDIDATES, NO_TOOLS_ARGS
# Reuse the existing HTML→text stripper for the description snippet we feed
# Haiku (safe on already-plain text too — HTMLParser just returns the text).
from scrapers.enrich.descriptions import _extract_text
# Reuse the display-time description cleaner's blocklist/heading logic so the
# tagger's excerpt is stripped of the same nav/cookie/ATS-metadata chrome the
# UI drops — one blocklist, no duplication. descfmt imports only re+markupsafe,
# so this stays a cheap import with no FastAPI dependency.
from web.descfmt import clean_text as _clean_desc_text, _looks_heading
# Deterministic posting-language detection (stopword/script heuristic, no LLM).
# Feeds the "PostingLanguage:" hint line in the payload and the lang_req merge
# backstop in _tag_batch — a German-language ad must never come back
# filterable as "English only" just because Haiku skipped the rubric line.
from lang_detect import detect_language as _detect_language

ROOT = os.path.dirname(os.path.abspath(__file__))
TAG_DEBUG_LOG = os.path.join(ROOT, "tag_debug.log")

MODEL = "claude-haiku-4-5"
# Batches now carry a richer description excerpt per role (opening + the
# requirements/profile section, up to ~3k chars — see _desc_excerpt), plus four
# extra description-derived fields per line. At ~750 input tokens/job that's
# ~3-4x the old title-only payload, so we cut the batch (20 -> 13) and lengthen
# the timeout to keep a full batch inside one CLI call reliably. 13 keeps each
# batch's input under ~10k tokens — comfortably below any single-call limit —
# while still amortising the per-call CLI startup across a dozen roles.
TIMEOUT_SECONDS = 180
BATCH_SIZE = 13
# Excerpt char budgets fed to Haiku per role. The opening carries the role
# summary; the requirements/profile section (located later in the body) carries
# the language/education/experience/start signals — so we splice both rather
# than feeding a flat first-N-chars window that usually stops before the
# requirements list. Caps keep the per-job payload bounded (fail-loud on runaway
# descriptions is not needed — clean_text already trims chrome).
EXCERPT_OPENING_CHARS = 1200      # role summary / lead
EXCERPT_SECTION_CHARS = 1500      # requirements/profile section (if found)
EXCERPT_TOTAL_CAP = 3000          # hard ceiling on opening+section
EXCERPT_NO_HEADING_CHARS = 2500   # flat window when no requirements heading found

# Retry budgets per scan: a high-failure batch splits into sub-batches, then
# singles, each bounded so a bad run can't fan out subprocesses without limit.
SUBBATCH_SIZE = 6
SUBBATCH_BUDGET = 5
SINGLE_BUDGET = 10
# Circuit-breaker: after this many consecutive fully-blank batches the CLI is
# dead (expired OAuth, quota, etc.) — stop calling it and blank the rest rather
# than fan out doomed subprocesses, each paying TIMEOUT_SECONDS against a corpse.
CIRCUIT_BREAKER_THRESHOLD = 3

# Controlled vocabularies. Anything outside these is coerced to the fallback so
# the UI facets never sprout one-off values.
# AREA = primary career track (one per role). DESK refines markets.
AREAS = {
    "markets", "quant", "research", "ibd", "capital-markets",
    "corporate-banking", "asset-management", "wealth", "private-equity",
    "debt", "risk", "actuarial", "middle-office", "consulting", "accounting",
    "other",
}
# Markets function (the old "desk"); "strats" = desk strategists/quants on the
# floor (kept distinct from the top-level Research *division* tab, which is
# area=research — the two used to collide on the word "research").
DESKS = {"trading", "sales", "structuring", "strats"}
SENIORITIES = {"intern", "graduate", "analyst", "associate", "manager"}
JOB_TYPES = {"job", "internship", "graduate-programme"}
REGIONS = {"Europe", "Americas", "APAC", "MEA"}
_REGION_BY_LOWER = {r.lower(): r for r in REGIONS}  # case-insensitive snap
WORK_MODES = {"onsite", "hybrid", "remote"}
# Languages we recognize as a REQUIRED skill beyond English (comma-separated,
# lowercase ISO-639-1). English is the baseline and never emitted. Anything off
# this set is dropped (not coerced to a neighbour — a bogus code is worse than
# an empty field for a filter facet). "en" is explicitly excluded on parse.
LANG_CODES = {
    "de", "fr", "it", "es", "nl", "da", "sv", "no", "fi", "pt", "pl",
    "zh", "ja", "ko", "ar", "ru", "tr", "el", "cs", "hu", "ro",
}
# Minimum required degree floor. "" = none stated. Ordered coarse→fine; the
# tagger emits the FLOOR (a "master's preferred" role whose hard floor is a
# bachelor emits bachelor, not master).
EDUCATION_LEVELS = {"bachelor", "master", "phd"}
# start_date is free-form-ish but normalized to one of: asap | YYYY | YYYY-MM.
# Validated by regex on parse; anything else → "". The month, when present, must
# be 01-12 (a bare \d{2} would wave through the model's "2026-13" glitches).
_START_DATE_RE = re.compile(r"^(asap|\d{4}(-(0[1-9]|1[0-2]))?)$")
_YOE_CLAMP_MAX = 30  # sane ceiling; a parsed value above this is a model glitch

# Country is the one location field with an unbounded free-text tail: Haiku emits
# "US"/"USA"/"United States", "UK"/"England", "UAE", localized names, and even
# dumps multi-country lists into the field. Region never duplicates because it's
# a closed enum; country needs the same treatment. This snaps the common finance
# hubs onto one canonical English name; unknowns pass through unchanged (they
# still show, just un-deduped — strictly better than today, and easy to grow).
_COUNTRY_CANON = {
    # United States
    "us": "United States", "usa": "United States", "u.s.": "United States",
    "u.s.a.": "United States", "united states": "United States",
    "united states of america": "United States", "america": "United States",
    # United Kingdom
    "uk": "United Kingdom", "u.k.": "United Kingdom", "england": "United Kingdom",
    "scotland": "United Kingdom", "wales": "United Kingdom",
    "great britain": "United Kingdom", "britain": "United Kingdom",
    "united kingdom": "United Kingdom",
    # United Arab Emirates
    "uae": "United Arab Emirates", "u.a.e.": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
    # Hong Kong
    "hong kong": "Hong Kong", "hong kong sar": "Hong Kong", "hongkong": "Hong Kong",
    "hong kong s.a.r.": "Hong Kong",
    # Germany
    "germany": "Germany", "deutschland": "Germany",
    # Switzerland
    "switzerland": "Switzerland", "schweiz": "Switzerland",
    "suisse": "Switzerland", "svizzera": "Switzerland",
    # Netherlands
    "netherlands": "Netherlands", "the netherlands": "Netherlands",
    "nederland": "Netherlands", "holland": "Netherlands",
    # Other common finance hubs / localized names
    "france": "France", "singapore": "Singapore", "sg": "Singapore",
    "ireland": "Ireland", "luxembourg": "Luxembourg", "luxemburg": "Luxembourg",
    "spain": "Spain", "españa": "Spain", "espana": "Spain",
    "italy": "Italy", "italia": "Italy", "japan": "Japan", "china": "China",
    "india": "India", "canada": "Canada", "australia": "Australia",
    "poland": "Poland", "polska": "Poland", "belgium": "Belgium",
    "belgië": "Belgium", "belgique": "Belgium", "sweden": "Sweden",
    "qatar": "Qatar", "saudi arabia": "Saudi Arabia",
    "brazil": "Brazil", "brasil": "Brazil", "mexico": "Mexico", "méxico": "Mexico",
    "portugal": "Portugal", "austria": "Austria", "österreich": "Austria",
}


def _canon_country(country: str) -> str:
    """Snap a free-text country onto a canonical English name. Handles the
    LLM's multi-country dumps ('Netherlands; United States; …') by taking the
    first entry; leaves unrecognized countries untouched."""
    c = (country or "").strip()
    if not c:
        return ""
    # Some postings dump a whole location list into the country field — take the
    # first segment (also turns 'Hong Kong SAR, China' → 'Hong Kong SAR' → HK).
    c = re.split(r"[;/|]|,\s", c)[0].strip()
    return _COUNTRY_CANON.get(c.lower(), c)


# City has the same free-text tail as country: Haiku emits the localized form
# ("Frankfurt am Main", "München", "Zürich") alongside the English/short form
# ("Frankfurt", "Munich", "Zurich"), which splits one city into two dropdown
# entries. Snap the known finance hubs onto one canonical spelling; unknowns
# pass through unchanged. Keyed lower-case, value is the display spelling.
_CITY_CANON = {
    "frankfurt am main": "Frankfurt",
    "münchen": "Munich",
    "muenchen": "Munich",
    "zürich": "Zurich",
    "zuerich": "Zurich",
    "köln": "Cologne",
    "koeln": "Cologne",
    "wien": "Vienna",
    "genève": "Geneva",
    "geneve": "Geneva",
    "genf": "Geneva",
    "mailand": "Milan",
    "dusseldorf": "Düsseldorf",  # majority spelling uses the umlaut (65 vs 5)
}


def _canon_city(city: str) -> str:
    """Snap a free-text city onto one canonical spelling so localized variants
    don't split into duplicate facet entries. Unknowns pass through unchanged."""
    c = (city or "").strip()
    if not c:
        return ""
    return _CITY_CANON.get(c.lower(), c)


# Deterministic internship detector (title-level). Internships are stored but
# hidden by default in the web app (job_type=internship); we don't want a Haiku
# mislabel to leak one into the default view, so this overrides the tagger.
_INTERNSHIP_RE = re.compile(
    r"\bintern(?:s|ship|ships)?\b"      # intern/interns/internship — NOT internal/international
    r"|praktik\w*"                       # Praktikum / Praktikant(in)
    r"|werkstud\w*"                      # Werkstudent(in)
    r"|working\s+student"
    r"|\bstudents\b"                     # plural "Students …" = campus/working-student programme (JPMC); singular avoided (Student Loan desks)
    r"|\bstagiair\w*|\bstagiaire\w*|\bstagist\w*"   # FR/IT intern
    r"|tirocin\w*"                       # IT internship
    r"|becari[oa]\w*|pr[aá]cticas?"      # ES internship
    r"|est[aá]gi\w*|estagi\w*"           # PT internship
    r"|summer\s+analyst"                 # banking internship term of art
    r"|off[-\s]?cycle"                   # off-cycle (internship)
    r"|\balternan(?:ce|t|te|ts|tes)\b"   # FR work-study (alternance / alternant)
    r"|spring\s+(?:week|insight)"        # UK spring week / spring insight
    # 'insight' ONLY as a programme, never bare (avoids 'Insight Investment' the
    # firm, 'Customer Insight Manager', 'Business Insights Associate').
    r"|insight\s+(?:programme|program|day|week|event|experience|internship|series|scheme)"
    # 'placement' ONLY as a student placement, never bare (avoids 'Private Placement').
    r"|(?:industrial|work|student|summer|year[-\s]?long)\s+placement"
    r"|placement\s+(?:year|programme|program|scheme)"
    r"|vacation\s+scheme",               # UK vacation scheme
    re.IGNORECASE,
)


def _enforce_internship(job: dict) -> None:
    """If the title is clearly internship-shaped, force the internship type so
    the web app's hide-by-default works regardless of what the tagger returned."""
    if _INTERNSHIP_RE.search(job.get("title") or ""):
        job["job_type"] = "internship"
        if job.get("seniority") in ("", "graduate", "analyst", "associate"):
            job["seniority"] = "intern"


# Managerial-RUNG marker (above associate, out of the user's junior scope).
# Catches a bare "Manager" / "Managerin" / "managerial" the tagger tends to
# soften to "associate". EXCLUDES role-noun "X Manager" titles (Portfolio/
# Investment/Fund/Asset/Relationship/Wealth/Product/Account/Project/Programme
# Manager) — those name a job kind, not the rung, and many are roles the user
# wants at any level.
_MANAGER_RE = re.compile(r"\bmanager(?:in)?\b|\bmanagerial\b", re.IGNORECASE)
_MANAGER_ROLE_NOUN_RE = re.compile(
    r"\b(?:portfolio|investment|fund|asset|relationship|wealth|client|product|"
    r"account|project|programme|program|category)\s+manager",
    re.IGNORECASE,
)


def _enforce_manager(job: dict) -> None:
    """Force seniority=manager when the title carries a managerial-RUNG 'Manager'
    (not a role-noun like 'Portfolio Manager'), so the web app hides it via the
    same show_senior toggle as YoE-walled roles. Never overrides intern."""
    title = job.get("title") or ""
    if not _MANAGER_RE.search(title) or _MANAGER_ROLE_NOUN_RE.search(title):
        return
    if job.get("seniority") in ("", "graduate", "analyst", "associate"):
        job["seniority"] = "manager"

_SYSTEM = """You tag finance job postings with structured labels for a filtering UI. For each role you get its Sector (the kind of firm), Company, Title, Location, and a short Description excerpt. Every firm here is one the user already WANTS — so do NOT judge fit. Your job is to (a) separate genuine front-office / investment roles from back-office & non-finance noise, and (b) label the front-office ones precisely. Be decisive; never invent labels.

AREA — exactly one of:
- markets          : front-office Sales & Trading of financial instruments — trading, market making, flow/exotics, institutional markets sales, structuring, S&T graduate programmes. Any SECONDARY sales/trading of credit/rates/FX/equities/commodities lives here regardless of product, incl. titles like "Credit Sales", "Credit Trading", "EM Sales", "Rates Trading" (selling/trading the instrument to investor clients is markets, NOT capital-markets). PHYSICAL & ENERGY COMMODITY trading is markets too: a Trader / Merchandiser / Originator / desk Structurer of power, gas, LNG/LPG, crude & oil products, coal, emissions/carbon, metals, freight/shipping or agricultural/soft commodities (grain, oilseeds, sugar, cocoa, coffee) at an energy utility's trading arm, a commodity merchant, or a trading house is markets — DESK=trading for a trader/merchandiser, DESK=structuring for deal origination/structuring on the trading floor, DESK=sales for wholesale commodity marketing to trading counterparties. Do NOT default a commodity/energy TRADER or ORIGINATOR to other. (Then set DESK.)
- quant            : quantitative/systematic INVESTMENT roles — quant research, quant trading, systematic strategies. Also index/benchmark STRUCTURING & methodology at an exchange or index provider (designing calculation formulas, index risk models, performance attribution, strategy simulation). A pure software / data-engineering / ML-platform / infrastructure engineer is NOT quant, even at a quant fund (→ other); index OPERATIONS / client services / data sales are NOT quant (→ other); only count a 'quant developer/researcher' clearly on a research or trading strategy team.
- research         : sell-side or independent investment research, economists, strategists (equity/credit/macro/FX/rates/commodities). NOT market / consumer / UX / academic research.
- ibd              : investment banking division — M&A, advisory, sector/sponsor coverage, leveraged finance.
- capital-markets  : PRIMARY ISSUANCE only — DCM/ECM origination, syndicate, new-issue capital-markets. A "Sales" or "Trading" title (secondary/flow) is markets even when the product is credit/EM/rates — NOT capital-markets.
- corporate-banking : the bank's corporate/commercial CLIENT-COVERAGE and lending franchise — corporate & institutional banking coverage, relationship/coverage managers for CORPORATE clients, global corporate bank, commercial banking, corporate lending/credit origination, and transaction banking (cash management, trade finance, treasury/liquidity services, payments & receivables SOLD to corporate clients). The defining test is a BANK serving CORPORATE clients via lending + transaction services. NOT M&A/advisory (→ ibd); NOT bond/equity new-issue origination (→ capital-markets); NOT buy-side private credit investing (→ debt); NOT secondary trading (→ markets); NOT retail/branch/SME-consumer banking or RETAIL relationship managers (→ other); NOT wealth/private banking for individuals (→ wealth).
- asset-management : INSTITUTIONAL buy-side portfolio/fund management of PUBLIC-market securities, incl. hedge-fund investment roles and discretionary commodity TRADING desks at funds/merchants. Also the OWN-BALANCE-SHEET investment function at a PENSION FUND, INSURER, or SOVEREIGN INVESTOR — managing the annuity/pension/reserve asset book (portfolio/asset/investment manager, investment analyst, transition/mandate management on the investment side). Private/retail WEALTH advisory → wealth, NOT here. Private-equity/private-credit investing → private-equity / debt, NOT here. Investment OPERATIONS / compliance / data-ops / member-services → other.
- private-equity    : buy-side PRIVATE-EQUITY investing — buyout, growth equity, venture capital, infrastructure/real-asset equity, secondaries, fund-of-funds, and portfolio-operations on the investment side, at a GP / PE firm or a fund's deal team. The defining test is INVESTING the fund's own capital in private companies/assets. NOT sell-side sponsor coverage or M&A advice for PE clients (→ ibd); NOT public-market institutional fund management (→ asset-management).
- debt              : buy-side PRIVATE CREDIT / direct lending — private debt, direct lending, mezzanine, unitranche, distressed & special situations, credit-fund investing, infrastructure/real-estate debt funds. The test is INVESTING/LENDING the fund's capital in private credit. NOT DCM/ECM origination or syndicate (→ capital-markets); NOT sell-side leveraged finance / lev-fin advisory (→ ibd); NOT secondary credit trading or flow credit on a bank/market-maker desk (→ markets).
- wealth            : private banking / wealth & investment management for INDIVIDUAL clients — financial advisor, relationship/wealth/client-portfolio manager, private banker, wealth-planning, investment counsellor, family-office advisory. The client is a person/family, not an institution. (Institutional fund management → asset-management.)
- risk             : FINANCIAL risk only — market, credit, or liquidity risk. Environmental/social/ESG, operational, model, IT, or other non-financial risk → other.
- actuarial        : actuarial roles at insurers, reinsurers, pension funds/schemes/insurers, or actuarial consultancies — actuary, actuarial analyst/consultant, pricing, reserving, capital & Solvency-II modelling, longevity/annuity/valuation, ALM actuarial. Actuarial work at a consultancy (e.g. "Oliver Wyman Actuarial") is actuarial, NOT consulting. NOT generic financial risk (→ risk).
- middle-office    : the control/support layer AROUND the trading floor — product control, valuation control / IPV, trade support, trade capture/confirmations done as a control function, collateral management, business management / COO office, front-office control, desk/business analysis supporting a trading business. NOT pure back-office settlements/recs (→ other), NOT financial risk (→ risk).
- consulting       : management/strategy consulting, transaction advisory / deal advisory / deal services, financial-advisory consulting roles.
- accounting       : the finance/accounting function — financial & management accounting, controlling, FP&A, financial/regulatory reporting, tax, treasury accounting, audit-of-accounts, bookkeeping. (A clearly market-facing dealing/ALM treasury role is markets, not accounting.)
- other            : EVERYTHING ELSE (the bucket the user filters OUT). See the exclusion list.

DESK (markets function) — only when AREA=markets (else empty): trading | sales | structuring | strats (desk strategists / desk quants embedded on a trading floor; NOT the sell-side research division — that is AREA=research).

CRITICAL — bias toward INCLUSION within finance. Every firm is wanted, so if a role is plausibly a front-office/investment role at that kind of firm, give it the right finance AREA. Use 'other' ONLY when the role is CLEARLY one of:
- Pure back-office & ops: KYC, AML, onboarding, settlements, clearing, fund accounting, reconciliation, custody, corporate actions, collateral OPERATIONS. (But product/valuation control, trade support, collateral management, COO/business-management → middle-office, NOT other.)
- Tech/IT: software/backend/frontend/data/ML/platform/devops/SRE/cyber/QA engineering, IT support. (BUT a "Quant Developer" on a research/trading team is quant, not other.)
- Corporate & support: HR, recruiting, legal, marketing, communications, PR, facilities, procurement, internal audit, compliance monitoring/surveillance. (Accounting / controlling / FP&A / tax / financial reporting now have their OWN area = accounting — do NOT put them in 'other'.)
- NON-MARKETS SALES: B2B / retail / product / account-management / business-development sales of physical goods, energy/power supply, software, or payments — very common at energy utilities, commodity merchants, and fintech. These are 'other'. DESK=sales is INSTITUTIONAL MARKETS SALES ONLY (selling financial products to investor clients).
- Apprenticeship / Ausbildung / dual-study vocational tracks (pre-degree; set TYPE=job, area usually 'other'). But a genuine internship / summer analyst / working-student / graduate role is NOT automatically 'other' — give it the SAME finance area as the equivalent full-time role, judged by function (a markets internship = markets, an IBD summer analyst = ibd, a quant PhD intern = quant); use 'other' only when its underlying function is back-office / tech / non-finance.
(Consulting / transaction-advisory / deal-advisory roles now have their OWN area = consulting — do NOT put them in 'other'.)
- NON-JOB postings: talent communities / pools / networks, recruiting or "discovery" events, "register your interest" / "expression of interest" / interest forms, fellowships, and generic candidate pipelines are NOT real openings → area=other, TYPE=job (never graduate-programme).

Use the SECTOR as the strongest hint:
- Prop Trading & Market Makers / Systematic HFs & Quant AMs / Discretionary Macro HFs → expect markets/quant/research/asset-management; a generic finance title here is usually front-office.
- Energy Utilities w/ Trading / Physical Commodity Merchants → real trading desks exist here. A Trader / Merchandiser / Originator / desk Structurer of power, gas, oil, LNG, coal, emissions, metals, freight or agri commodities is front-office markets (or asset-management for a fund's discretionary commodity desk) — NOT other. Reserve 'other' for the genuine non-trading noise these firms also post in bulk: plant/asset operators, maintenance/field/grid/automation engineers, RETAIL or B2B energy-SUPPLY sales & account management, customer service, and cargo/commodity scheduling, LOGISTICS & operations (e.g. an "Operator"/"Confirmations"/"Contracts" role is trade OPS → middle-office or other, not a trader). Judge by whether the role sits on the trading desk (markets) or is supply/retail/plant/ops (other).
- Global / European & Regional Banks → all areas exist; read title + description.
- Pensions, Insurance & Sovereign Investors → the firm's OWN investment/portfolio-management of its annuity/pension/reserve book = asset-management (or markets for a dealing desk); actuarial/pricing/reserving/capital-modelling/longevity = actuarial. Pension administration, member services, onboarding, claims, underwriting-ops = other. Do NOT default this sector's investment or actuarial roles to other.
- Private Equity & Private Markets → expect private-equity (deal/investment teams) and debt (private credit / direct lending), plus some asset-management (multi-asset/secondaries) and corporate/support 'other'. An investment-team role here is almost never ibd.
- Consulting & Advisory → expect consulting (strategy, deal/transaction advisory, restructuring, valuation, economic consulting); audit/tax/assurance/bookkeeping → accounting; the usual tech/HR/ops noise → other. A clearly corporate-finance/M&A-advisory desk → ibd.
- Corporate Treasury → mostly corporate cash-management / FP&A → accounting, unless a clearly market-facing dealing/ALM role (→ markets).
- MDBs, Central Banks & IFIs → research/economist (research) or markets/policy operations.

SENIORITY — the rung, one of: intern | graduate | analyst | associate | manager. intern = internship / working-student / any student-programme entry: also a placement (industrial/summer/work placement, placement year), a spring week / spring insight / insight programme, or an alternance/alternant (FR work-study) — all of these are intern, NOT analyst/graduate. (An apprenticeship / Ausbildung / dual-study vocational track is NOT this — leave it as its own type=job.) graduate = grad scheme / fresh-grad entry. analyst = the standard junior bank/markets rung (0-3 yrs). associate = the next rung up (post-analyst / MBA / ~3-6 yrs) — still in scope. manager = a managerial / 5+ yrs rung ABOVE associate (e.g. "Manager", "Senior Manager", or a body demanding 5+ years) — OUT of the user's junior scope, but still label it so the UI can hide it. Default to analyst when an entry-level role gives no clearer signal; only use manager when the title/body clearly signals that rung. NOTE: "Portfolio/Investment/Fund/Asset/Relationship Manager" are ROLE nouns, not the managerial rung — judge their seniority from the rest of the title, not the word "Manager".
TYPE — one of: job, internship, graduate-programme. internship = any student/pre-graduation programme: internship, summer/off-cycle internship, working-student/Werkstudent, industrial/summer/work placement or placement year, spring week / spring insight / insight programme, alternance/alternant. These are TYPE=internship even when the title also says "programme" — do NOT call them graduate-programme. graduate-programme = a named, structured graduate/rotational SCHEME for people who have ALREADY graduated (e.g. "Markets Graduate Programme", "S&T Analyst Programme"). A normal entry-level role, a plain "Graduate Analyst" title, an "Engineer in Training", or a non-job posting is TYPE=job — NOT graduate-programme.
LOCATION — normalize: CITY (English; repeat country if only country given; empty if remote/unknown), COUNTRY (canonical English short name, NEVER an abbreviation or localized form: "United States" not "US/USA", "United Kingdom" not "UK/England", "United Arab Emirates" not "UAE", "Germany" not "Deutschland"; if a role spans several countries give only the primary one, never a list), REGION (one of Europe/Americas/APAC/MEA, else empty; HK/Singapore/Tokyo/Shanghai/Sydney/Mumbai/Kazakhstan/Uzbekistan/Central Asia=APAC; London/Frankfurt/Paris/Zurich=Europe; New York/Toronto/São Paulo=Americas; Dubai/Riyadh/Doha/Johannesburg/Africa/Middle East=MEA).
WORKMODE — onsite | hybrid | remote (default onsite when a city is named and nothing says otherwise).

The next four fields come FROM THE DESCRIPTION EXCERPT (Sector/Company/Title/Location can't tell you these). Read the requirements/profile section — which the excerpt splices in under [REQUIREMENTS] when present. If the excerpt is "(none)", output `-` for all four.
LANG_REQ — languages REQUIRED for the role BEYOND English, as comma-separated lowercase ISO codes from EXACTLY this set: de fr it es nl da sv no fi pt pl zh ja ko ar ru tr el cs hu ro. Only HARD requirements count: "fluent German required", "native-level French", "Dutch and French mandatory", "must speak Italian". A language described as "a plus / nice to have / preferred / advantageous / an asset / desirable" does NOT count — output nothing for it. English is the baseline — NEVER emit `en`. If the posting is written ENTIRELY in a non-English language (e.g. the whole excerpt is German or French prose), that language IS required — emit its code even without an explicit statement; a `PostingLanguage:` line in the payload is a detected hint of exactly this case — include that code. Empty (`-`) = English-only or no language stated.
MIN_YOE — the minimum years of full-time PROFESSIONAL experience the role explicitly requires, as an integer. A range → the LOWER bound ("3-5 years of experience" → 3, "0-3 years" → 0). "minimum 5 years" / "at least 5 years" / "5+ years" → 5. Internship / working-student / new-grad / graduate-programme / entry-level → 0. If no explicit experience requirement is stated → 0 (never guess from seniority). Cap at 30.
EDUCATION — the MINIMUM degree explicitly REQUIRED: bachelor | master | phd, or `-` if none is stated. Emit the required FLOOR: "Master's preferred, Bachelor's required" → bachelor; "Master's or equivalent required" → master; "PhD in a quantitative field" → phd. "Master's preferred" with no hard floor → `-`. For a student internship that requires being enrolled ("currently pursuing a Bachelor's" / "enrolled in a Master's programme"), emit the degree being PURSUED (bachelor / master). A bare "degree" / "university degree" with no level → bachelor.
START_DATE — the normalized start date when stated: `YYYY-MM` if a month or season is given (summer→06, autumn/fall→09, winter→01, spring→03), `YYYY` if only a year, `asap` for immediate / "as soon as possible" / "start immediately", `-` if unstated. Use the year from the posting context (e.g. a "September 2026" intake → 2026-09).

INPUT: numbered job blocks separated by ===.
OUTPUT: one line per job, SAME order, EXACTLY this and nothing else (no header, no commentary, no code fences):
INDEX|AREA|DESK|SENIORITY|TYPE|CITY|COUNTRY|REGION|WORKMODE|LANG_REQ|MIN_YOE|EDUCATION|START_DATE
CRITICAL: every line has EXACTLY 13 fields (12 pipes). For ANY field that is empty/not-applicable, output a single hyphen `-` — NEVER leave it blank and NEVER collapse two empty fields into one. The number of `|` per line is always 12. MIN_YOE is always a number (0 when none).

Examples:
0|markets|trading|graduate|graduate-programme|London|United Kingdom|Europe|onsite|-|0|bachelor|2026-09
1|other|-|analyst|job|Essen|Germany|Europe|onsite|de|0|-|-
2|quant|-|analyst|job|Hong Kong|Hong Kong|APAC|hybrid|-|3|phd|asap
3|ibd|-|analyst|job|Frankfurt|Germany|Europe|onsite|de|0|bachelor|-
4|asset-management|-|associate|job|Geneva|Switzerland|Europe|onsite|fr|5|master|-
5|markets|trading|analyst|job|Geneva|Switzerland|Europe|onsite|-|3|-|2026
6|ibd|-|analyst|job|Frankfurt|Germany|Europe|onsite|-|0|master|2026-06
7|accounting|-|graduate|job|Dublin|Ireland|Europe|hybrid|-|0|bachelor|-
(Row 6's `2026-06` came from a stated "summer 2026" start; row 0's `2026-09` from a "September 2026" intake.)
"""


def _fresh_health() -> dict:
    return {
        "cli_path": None,
        "model": MODEL,
        "batches_total": 0,
        "batches_ok": 0,
        "batches_failed": 0,
        "subbatch_retries": 0,
        "single_retries": 0,
        "failure_reasons": [],
        "total_latency_s": 0.0,
        "jobs_total": 0,
        "jobs_tagged": 0,
        # Set True when the circuit breaker trips (CLI confirmed dead mid-run).
        "cli_down": False,
        # Set True when the run switched to the paid direct-API fallback after
        # the breaker tripped (OAuth dead but ANTHROPIC_TAG_API_KEY carried it).
        "api_fallback": False,
    }


LAST_RUN_HEALTH: dict = _fresh_health()

# Tag keys written onto each job dict / persisted via db.set_tags.
TAG_KEYS = (
    "area", "desk",
    "seniority", "job_type",
    "loc_city", "loc_country", "loc_region", "work_mode",
    "lang_req", "education", "start_date", "min_yoe",
)


def _blank_tags(job: dict) -> None:
    """Leave a job untagged but well-formed (filterable as 'unclassified')."""
    job["area"] = ""
    job["desk"] = ""
    job["seniority"] = ""
    job["job_type"] = "job"
    job["loc_city"] = ""
    job["loc_country"] = ""
    job["loc_region"] = ""
    job["work_mode"] = ""
    # Description-derived facets. On a blank (CLI-failure) tag we leave these as
    # the "not determined" sentinel so the caller doesn't persist a false ''
    # ("English-only / none required") over a genuinely unknown row. NULL is the
    # marker for "tagged pre-description" that the nightly re-tag hook keys off;
    # None here maps to a SQL NULL via the db layer.
    job["lang_req"] = None
    job["education"] = None
    job["start_date"] = None
    # min_yoe stays absent on a blank tag — main.py/backfill fall back to the
    # regex value; we don't zero it here.


# Requirements/profile headings whose section carries the language / education /
# experience / start-date signals. A flat first-N-chars window usually stops in
# the role summary before this list, so we locate it and splice it in. Matched
# against the heading text detected by descfmt's _looks_heading (which already
# recognises "Your profile", "Qualifications", "Requirements", "What you'll
# bring", ALL-CAPS variants, etc.). We narrow to the subset that reliably holds
# the requirements — a "What we offer" heading is a perks list, not signal.
_REQ_HEADING_RE = re.compile(
    r"(?:your\s+profile"
    r"|(?:minimum |preferred |basic |desired |key )?qualifications"
    r"|requirements"
    r"|(?:key )?skills"
    r"|what (?:you|we)(?:’|')?ll (?:bring|need|looking for)"
    r"|what we(?:’|')?re looking for"
    r"|we are looking for"
    r"|who you are"
    r"|(?:candidate |ideal )?(?:profile|candidate)"
    r"|experience"
    r"|education"
    r"|essential"
    r")",
    re.IGNORECASE,
)


def _requirements_section(lines: list[str]) -> str:
    """From cleaned description lines, return the requirements/profile section:
    text from the FIRST requirements-style heading onward, capped at
    EXCERPT_SECTION_CHARS. Empty if no such heading is found. We take from the
    heading to the end (not to the next heading) because the signals we want —
    languages, degree floor, YoE, start date — often trail into an adjacent
    "what we offer" block, and the char cap bounds it anyway."""
    for i, line in enumerate(lines):
        if not line:
            continue
        if _looks_heading(line) and _REQ_HEADING_RE.search(line):
            section = "\n".join(lines[i:]).strip()
            return section[:EXCERPT_SECTION_CHARS]
    return ""


def _desc_excerpt(job: dict) -> str:
    """Cleaned, spliced description excerpt for the tag payload.

    Replaces the old raw first-800-chars window. We clean the raw description
    with descfmt's line cleaner (drops nav/cookie/ATS-metadata chrome — the same
    blocklist the UI uses), then build: the opening summary (EXCERPT_OPENING_CHARS)
    PLUS the requirements/profile section spliced from later in the body
    (EXCERPT_SECTION_CHARS), total capped at EXCERPT_TOTAL_CAP. When no
    requirements heading exists we fall back to a flat EXCERPT_NO_HEADING_CHARS
    window. This is what feeds the description-derived facets (lang_req /
    education / min_yoe / start_date), which almost always live in that
    requirements list rather than the lead paragraph."""
    raw = job.get("description") or ""
    if not raw:
        return ""
    # HTML-strip first (old Greenhouse rows are HTML; no-op on plain text), then
    # run the display blocklist so we don't spend the char budget on chrome.
    text = _clean_desc_text(_extract_text(raw, max_chars=16000),
                            job.get("title"))
    if not text:
        return ""
    lines = text.split("\n")
    opening = text[:EXCERPT_OPENING_CHARS].strip()
    section = _requirements_section(lines)
    if not section:
        # No requirements heading — a flat window is the honest best-effort.
        return text[:EXCERPT_NO_HEADING_CHARS].strip()
    # Avoid duplicating text when the requirements section IS the opening (short
    # postings where the first heading sits inside the first 1,200 chars).
    if section[:80] and section[:80] in opening:
        return text[:EXCERPT_TOTAL_CAP].strip()
    combined = f"{opening}\n\n[REQUIREMENTS]\n{section}"
    return combined[:EXCERPT_TOTAL_CAP].strip()


def _build_payload(jobs: list[dict]) -> str:
    blocks = []
    for idx, job in enumerate(jobs):
        desc = _desc_excerpt(job)
        # Detected posting language: shown to the model as a hint AND stashed
        # on the job for the deterministic lang_req merge after parsing.
        lang = _detect_language(desc) if desc else ""
        job["_desc_lang"] = lang
        lang_line = f"PostingLanguage: {lang}\n" if lang else ""
        blocks.append(
            f"INDEX: {idx}\n"
            f"Sector: {job.get('category', '') or '(unknown)'}\n"
            f"Company: {job.get('company', '')}\n"
            f"Title: {job.get('title', '')}\n"
            f"Location: {job.get('location', '') or '(unspecified)'}\n"
            f"{lang_line}"
            f"Description: {desc or '(none)'}"
        )
    return "\n===\n".join(blocks)


def merge_detected_lang(lang_req: str | None, detected: str) -> str | None:
    """Union the detected posting language into a parsed lang_req value.

    Deterministic backstop for the "written in X ⇒ X required" rubric: applied
    only to successfully-parsed rows (a None from a failed tag stays None so
    the NULL re-tag semantics survive). Keeps the comma-joined codes sorted,
    matching _coerce_lang_req's output format."""
    if not detected or lang_req is None:
        return lang_req
    codes = {c for c in lang_req.split(",") if c}
    codes.add(detected)
    return ",".join(sorted(codes))


_LINE_RE = re.compile(r"^\s*(\d+)\s*\|(.*)$")


def _coerce(area: str, desk: str, seniority: str, job_type: str, region: str,
            work_mode: str) -> tuple[str, str, str, str, str, str]:
    """Snap free-text labels onto the controlled vocabularies."""
    area = area if area in AREAS else "other"
    desk = desk if desk in DESKS else ""
    if area != "markets":
        desk = ""  # markets function only meaningful within markets
    seniority = seniority if seniority in SENIORITIES else ""
    job_type = job_type if job_type in JOB_TYPES else "job"
    # The intern rung IS an internship by definition (the seniority vocab defines
    # intern = internship/working-student). Haiku flip-flops job_type on foreign
    # internship titles the regex can't catch (bare "Stage", "Praktikum" variants
    # the title doesn't spell out) — when it nails the rung but mislabels the
    # type, couple them so the Full-time/Internships filter stays correct.
    if seniority == "intern":
        job_type = "internship"
    # Case-insensitive: every other field is lowercased before _coerce, but
    # region is matched against capitalized labels (Europe/APAC/…), so a model
    # reply of "europe"/"apac" used to snap to "". Map via the lowercase index.
    region = _REGION_BY_LOWER.get(region.strip().lower(), "")
    work_mode = work_mode if work_mode in WORK_MODES else ""
    return (area, desk, seniority, job_type, region, work_mode)


def _coerce_lang_req(raw: str) -> str:
    """Snap the comma-separated language field onto the controlled ISO codes.
    Drops English (the baseline) and anything off-vocab; dedupes preserving
    order. Off-vocab codes are dropped rather than kept — a facet dropdown can't
    afford one-off values, and a bogus code is worse than an empty field."""
    if not raw:
        return ""
    out: list[str] = []
    for tok in raw.replace(";", ",").split(","):
        code = tok.strip().lower()
        if code in LANG_CODES and code not in out:
            out.append(code)
    return ",".join(out)


def _coerce_start_date(raw: str) -> str:
    """Validate the start-date field against ^(asap|YYYY(-MM)?)$; else ''.
    A dated value must not be in the past — postings sometimes carry a stale
    'posted'/'updated' date the model mistakes for a start (seen live:
    2022-05), and a bygone start date is noise, not a facet. Anything from
    last year back, or absurdly far out, coerces to ''."""
    s = (raw or "").strip().lower()
    if not _START_DATE_RE.match(s):
        return ""
    if s != "asap":
        year = int(s[:4])
        # DB timestamps are UTC — validate against the UTC year, not naive local.
        this_year = datetime.now(timezone.utc).year
        if year < this_year or year > this_year + 4:
            return ""
    return s


def _coerce_min_yoe(raw: str) -> int:
    """Parse the min-YoE field to an int, clamped 0..30. Non-numeric → 0."""
    m = re.search(r"\d+", raw or "")
    if not m:
        return 0
    return max(0, min(_YOE_CLAMP_MAX, int(m.group(0))))


def parse_response(text: str, expected_count: int) -> dict[int, dict]:
    """Parse the CLI output into {index: tag_dict}. Lines that don't match or
    have the wrong field count are skipped (those jobs stay untagged)."""
    results: dict[int, dict] = {}
    for raw in text.splitlines():
        m = _LINE_RE.match(raw)
        if not m:
            continue
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        if not (0 <= idx < expected_count):
            continue
        # `-` is the explicit empty sentinel (keeps the field count stable when
        # optional fields are empty — the model otherwise collapses adjacent
        # blanks and drops a field). Normalize it back to "".
        fields = [("" if f.strip() == "-" else f.strip())
                  for f in m.group(2).split("|")]
        # 12 fields now: the original 8 + lang_req|min_yoe|education|start_date.
        if len(fields) != 12:
            continue
        (area, desk, seniority, job_type,
         city, country, region, work_mode,
         lang_req, min_yoe, education, start_date) = fields
        (area, desk, seniority, job_type, region, work_mode) = _coerce(
            area.lower(), desk.lower(), seniority.lower(), job_type.lower(),
            region, work_mode.lower(),
        )
        education = education.lower()
        results[idx] = {
            "area": area,
            "desk": desk,
            "seniority": seniority,
            "job_type": job_type,
            "loc_city": _canon_city(city),
            "loc_country": _canon_country(country),
            "loc_region": region,
            "work_mode": work_mode,
            # Description-derived facets. All degrade to "" (not None) on a
            # successfully-parsed line: the line HAD a description behind it, so
            # "" here means "genuinely none required", distinct from the NULL a
            # pre-description tag leaves.
            "lang_req": _coerce_lang_req(lang_req),
            "education": education if education in EDUCATION_LEVELS else "",
            "start_date": _coerce_start_date(start_date),
            "min_yoe": _coerce_min_yoe(min_yoe),
        }
    return results


def _log_debug(label: str, stdout: str, stderr: str) -> None:
    try:
        with open(TAG_DEBUG_LOG, "a") as fp:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fp.write(f"\n=== {ts} {label} ===\nSTDOUT:\n{stdout or '(empty)'}"
                     f"\nSTDERR:\n{stderr or '(empty)'}\n")
    except OSError:
        pass


def _tag_batch(batch: list[dict], bin_path: str, health: dict | None = None) -> None:
    """Tag one batch in place. Sets tag keys on every job; failures leave the
    job's tags blank and are recorded in `health`."""
    if health is not None:
        health["batches_total"] += 1
        if health.get("cli_path") is None:
            health["cli_path"] = bin_path

    payload = _build_payload(batch)
    start = time.monotonic()
    try:
        # NO_TOOLS_ARGS + neutral cwd: the payload embeds scraped job text
        # (hostile input); the tagger needs no tools and no project-root cwd.
        result = subprocess.run(
            [bin_path, "-p", *NO_TOOLS_ARGS, "--model", MODEL, _SYSTEM],
            input=payload, capture_output=True, text=True,
            timeout=TIMEOUT_SECONDS, cwd=tempfile.gettempdir(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        reason = (f"claude CLI timed out after {TIMEOUT_SECONDS}s"
                  if isinstance(exc, subprocess.TimeoutExpired)
                  else f"claude CLI launch failed: {exc}")
        for j in batch:
            _blank_tags(j)
        if health is not None:
            health["batches_failed"] += 1
            health["failure_reasons"].append(reason)
            health["total_latency_s"] += time.monotonic() - start
        _log_debug(f"FAIL batch={len(batch)}", "", reason)
        return

    elapsed = time.monotonic() - start
    if health is not None:
        health["total_latency_s"] += elapsed

    if result.returncode != 0:
        err = (result.stderr or "")[:200].strip() or "unknown error"
        reason = f"claude CLI exit {result.returncode}: {err}"
        for j in batch:
            _blank_tags(j)
        if health is not None:
            health["batches_failed"] += 1
            health["failure_reasons"].append(reason)
        _log_debug(f"EXIT {result.returncode} batch={len(batch)}",
                   result.stdout, result.stderr)
        return

    _apply_parsed(batch, result.stdout, result.stderr, health)


def _apply_parsed(batch: list[dict], out_text: str, err_text: str,
                  health: dict | None) -> None:
    """Parse a model response and apply tags to the batch — shared tail of
    both transports (CLI subprocess and direct-API fallback)."""
    parsed = parse_response(out_text, len(batch))
    for i, job in enumerate(batch):
        tags = parsed.get(i)
        if tags is None:
            _blank_tags(job)
        else:
            job.update(tags)
            job["lang_req"] = merge_detected_lang(
                job.get("lang_req"), job.get("_desc_lang", ""))

    missing = sum(1 for i in range(len(batch)) if i not in parsed)
    if health is not None:
        if missing == 0:
            health["batches_ok"] += 1
        else:
            health["batches_failed"] += 1
            health["failure_reasons"].append(
                f"parse-fail: {missing}/{len(batch)} untagged"
            )
            _log_debug(f"PARSE-FAIL {missing}/{len(batch)} untagged",
                       out_text, err_text)


def _api_key() -> str:
    """ANTHROPIC_TAG_API_KEY from the environment or the repo .env. Used ONLY
    as a fallback transport when the subscription CLI is down (expired OAuth) —
    it bills the paid Console account, so it must never become the primary."""
    key = os.environ.get("ANTHROPIC_TAG_API_KEY", "")
    if key:
        return key
    try:
        with open(os.path.join(ROOT, ".env")) as fp:
            for line in fp:
                line = line.strip()
                if line.startswith("ANTHROPIC_TAG_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def _tag_batch_api(batch: list[dict], api_key: str,
                   health: dict | None = None) -> None:
    """Tag one batch via the Messages API directly. Deliberately bypasses the
    `claude` CLI: with both OAuth and an API key present the CLI's auth
    precedence is ambiguous, and the whole point of this path is that OAuth is
    dead. Same payload, same system prompt, same parser as the CLI transport."""
    import requests

    if health is not None:
        health["batches_total"] += 1

    payload = _build_payload(batch)
    start = time.monotonic()
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 4096,
                "system": _SYSTEM,
                "messages": [{"role": "user", "content": payload}],
            },
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        out_text = "".join(
            block.get("text", "")
            for block in resp.json().get("content", [])
            if block.get("type") == "text"
        )
    except Exception as exc:
        reason = f"API fallback failed: {type(exc).__name__}: {exc}"
        for j in batch:
            _blank_tags(j)
        if health is not None:
            health["batches_failed"] += 1
            health["failure_reasons"].append(reason)
            health["total_latency_s"] += time.monotonic() - start
        _log_debug(f"API-FAIL batch={len(batch)}", "", reason)
        return

    if health is not None:
        health["total_latency_s"] += time.monotonic() - start
    _apply_parsed(batch, out_text, "", health)


def _tag_batch_any(batch: list[dict], bin_path: str,
                   health: dict | None = None) -> None:
    """Transport dispatcher: direct API once the run has fallen back, CLI
    otherwise — so retries inside a fallen-back run don't hit the dead CLI."""
    if health is not None and health.get("api_fallback"):
        _tag_batch_api(batch, _api_key(), health=health)
    else:
        _tag_batch(batch, bin_path, health=health)


def _is_untagged(job: dict) -> bool:
    # A job is "untagged" if the tagging pass left area empty (the model always
    # emits an area for a successful row, even if 'other').
    return not job.get("area")


def _retry_batch(batch: list[dict], bin_path: str, health: dict) -> None:
    """Sub-batch + single retry for jobs the first pass left untagged.
    No-op once the circuit breaker has tripped — a dead CLI won't fix a batch,
    so retrying only burns more doomed subprocess calls."""
    if health.get("cli_down"):
        return
    failed = [j for j in batch if _is_untagged(j)]
    for i in range(0, len(failed), SUBBATCH_SIZE):
        if health["subbatch_retries"] >= SUBBATCH_BUDGET:
            break
        chunk = failed[i:i + SUBBATCH_SIZE]
        health["subbatch_retries"] += 1
        _tag_batch_any(chunk, bin_path, health=health)

    for j in [j for j in batch if _is_untagged(j)]:
        if health["single_retries"] >= SINGLE_BUDGET:
            break
        health["single_retries"] += 1
        _tag_batch_any([j], bin_path, health=health)


def tag_jobs(jobs: list[dict]) -> list[dict]:
    """Tag every job in-place with the TAG_KEYS facets. Safe on an empty list.
    Resets and populates module-level LAST_RUN_HEALTH."""
    global LAST_RUN_HEALTH
    LAST_RUN_HEALTH = _fresh_health()
    health = LAST_RUN_HEALTH
    health["jobs_total"] = len(jobs)

    # tag_debug.log is append-only across every scan and retry; rotate like
    # deliver.log (5MB -> .1) so it can't grow unbounded over a year.
    try:
        if os.path.getsize(TAG_DEBUG_LOG) > 5 * 1024 * 1024:
            os.replace(TAG_DEBUG_LOG, TAG_DEBUG_LOG + ".1")
    except OSError:
        pass

    if not jobs:
        return jobs

    bin_path = _claude_bin()
    if not bin_path:
        reason = ("claude CLI not found at any of "
                  + ", ".join(CLAUDE_BIN_CANDIDATES[:-1]))
        for j in jobs:
            _blank_tags(j)
            _enforce_internship(j)
            _enforce_manager(j)
        health["failure_reasons"].append(reason)
        return jobs

    health["cli_path"] = bin_path

    # Circuit-breaker state: count consecutive fully-blank batches. Once the CLI
    # is confirmed down we blank the rest without any further subprocess calls.
    consecutive_blank = 0
    for start in range(0, len(jobs), BATCH_SIZE):
        batch = jobs[start:start + BATCH_SIZE]
        if health.get("cli_down"):
            for j in batch:
                _blank_tags(j)
            continue
        _tag_batch_any(batch, bin_path, health=health)
        if all(_is_untagged(j) for j in batch):
            # A fully-blank batch signals CLI death, not a partial parse miss —
            # feed the breaker and skip the retry fan-out (retrying a dead CLI
            # only burns SUBBATCH_BUDGET+SINGLE_BUDGET more doomed subprocesses).
            consecutive_blank += 1
            if consecutive_blank >= CIRCUIT_BREAKER_THRESHOLD:
                # Before declaring the run dead, try the paid direct-API
                # fallback (ANTHROPIC_TAG_API_KEY in .env). If it tags this
                # batch, the rest of the run — including retries — rides the
                # API; earlier blanked batches self-repair via the nightly
                # desc-facet hook (blank tags leave lang_req NULL). No key or
                # a dead API → the old blank-the-rest behavior.
                if not health.get("api_fallback") and _api_key():
                    print("\nWARN: claude CLI appears down — switching to "
                          "direct-API fallback (ANTHROPIC_TAG_API_KEY)",
                          flush=True)
                    health["api_fallback"] = True
                    _tag_batch_api(batch, _api_key(), health=health)
                    if not all(_is_untagged(j) for j in batch):
                        consecutive_blank = 0
                        continue
                health["cli_down"] = True
                reason = (f"{consecutive_blank} consecutive batches returned "
                          "fully untagged — claude CLI appears to be down "
                          "(expired auth, quota exhausted, or unresponsive)"
                          + (" and the API fallback also failed"
                             if health.get("api_fallback") else
                             " and no ANTHROPIC_TAG_API_KEY fallback is set")
                          + ". Blanking the rest without further calls.")
                health["failure_reasons"].append(reason)
                print(f"\nERROR: {reason}", flush=True)
            continue
        consecutive_blank = 0
        missing = sum(1 for j in batch if _is_untagged(j))
        if missing > len(batch) // 2 and missing > 0:
            _retry_batch(batch, bin_path, health)

    for j in jobs:
        _enforce_internship(j)
        _enforce_manager(j)
        j.pop("_desc_lang", None)  # payload-time scratch, not a tag column
    health["jobs_tagged"] = sum(1 for j in jobs if not _is_untagged(j))
    return jobs
