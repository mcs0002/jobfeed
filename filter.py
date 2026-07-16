"""
Negative-only filter — every job passes unless it hits one of the buckets
below. The keyword filter is no longer trying to recognise "good" titles;
it just removes the obvious noise before the role is stored and tagged. The
Haiku tagger (tag.py) assigns the area/desk facets the web app filters on.

Each bucket is a flat list/set the user can edit without touching control
flow. Add a line, drop a line — `is_relevant` stays the same. Order of
checks (most-likely-to-trip first) is a small latency optimisation only;
swapping the order doesn't change the result.

Hard rules that aren't simple substring matches stay as regexes at the
bottom: dual-rung Analyst/Associate, YoE walls, and the experience-wall
backstop used by main.py BEFORE scoring.
"""
import os
import re


# Filtering model. The web app now does sector/function filtering interactively,
# so the scrape-time filter only enforces the hard gates that should never be
# stored: back-office *locations*, senior rungs, internships (deferred), and
# YoE walls. The function-level buckets (back-office / tech / non-finance / IBD)
# are kept but applied only in STRICT mode — by default they pass through so
# the role is stored and tagged for site-side filtering ("store the rest").
# Flip on with JOBS_STRICT_FILTER=1 to restore the old aggressive behaviour.
STRICT_FUNCTION_FILTER = os.environ.get("JOBS_STRICT_FILTER", "").lower() in (
    "1", "true", "yes", "on",
)


# Locations that are back-office centers — filter out globally regardless
# of employer category. Front-office markets seats don't sit here. China
# is intentionally NOT excluded — the user wants Shanghai / Beijing / HK
# / Shenzhen / Guangzhou in scope.
LOCATION_DROPS = {
    # India — blanket exclusion (country + all major hubs)
    "india",
    "mumbai", "pune", "noida", "chennai", "bengaluru", "bangalore",
    "gurgaon", "gurugram", "hyderabad", "kolkata", "delhi", "new delhi",
    "ahmedabad", "jaipur",
    # Other established back-office centers
    "manila",
    "warsaw", "warszawa", "krakow", "kraków", "lodz", "łódź",
}


# Senior-rung markers, dropped at scrape time always. "Associate" is NOT here:
# the rung is firm-dependent (senior at banks, ENTRY at PE/AM/consulting), so it
# is stored by default and the tagger's seniority + the web app's "Include
# associate roles" toggle sort it. Strict mode (JOBS_STRICT_FILTER=1) still drops
# bare associate via _ASSOCIATE_STRICT_DROP below, minus dual-rung titles.
SENIOR_TITLE_DROPS = {
    # English
    "senior", "sr.", "sr ",
    "lead", "principal", "staff",
    "vice president", "vp", "avp", "svp", "evp",
    "director", "managing director", "md ",
    "head of", "head,", "executive director", "ed",
    "managing partner", "partner ",
    "chief", "cxo", "ceo", "cfo", "coo", "cto", "cio",
    # German Landesbank / Sparkasse / corporate titles
    "leitung", "leiter", "leiterin",
    "gruppenleitung", "gruppenleiter", "gruppenleiterin",
    "abteilungsleitung", "abteilungsleiter", "abteilungsleiterin",
    "bereichsleitung", "bereichsleiter", "bereichsleiterin",
    "teamleitung", "teamleiter", "teamleiterin",
    "experte", "expertin",
    "geschäftsführer", "geschäftsführerin", "geschäftsführung",
    "vorstand", "vorständin",
}


# Back-office operations / control / settlement. Includes "trade support"
# and "middle office" — both are post-trade, NOT front-office markets,
# regardless of whether "Markets" or "Global Markets" appears in the team
# name.
BACK_OFFICE_DROPS = {
    "kyc", "aml ", "anti-money laundering",
    "transaction monitoring", "sanctions screening",
    "financial crime", "fraud analyst", "fraud investigator",
    "know your customer", "customer due diligence", "cdd ",
    "fund accounting", "fund administration", "fund admin",
    "fund servicing", "asset servicing", "securities servicing",
    "settlements", "settlement analyst", "reconciliation",
    "securities operations",
    "custody", "transfer agency", "ta operations",
    "trade support", "trade operations", "trade services",
    "trading services", "transactions specialist", "middle office",
    "operations analyst", "ops analyst", "back office",
    "regulatory reporting", "surveillance",
    "control room",
    "client services associate", "client service operations",
}


# Retail / consumer / branch banking. Universal banks (Citi, Chase, BofA) bury
# the front office under thousands of these; none is wanted. "Relationship
# manager" is deliberately ABSENT — corporate RM/coverage is ambiguous (could be
# the corporate-markets work the user wants), so it's left to tag + site filter.
RETAIL_DROPS = {
    "teller", "personal banker", "consumer banker", "retail banker",
    "branch manager", "branch operations", "branch associate",
    "private client banker", "premier banker", "relationship banker",
    "financial center", "store manager", "store supervisor",
    "loan officer", "mortgage loan",
    "consumer lending", "card services", "collections specialist",
    # Retail/consumer-bank titles that don't use "banker"/"branch" wording
    # (TD Securities/Wells Fargo full-board audit, July 2026). "banking
    # associate" itself is deliberately NOT here — it substring-matches
    # "Investment/Corporate/Private Banking Associate", which are wanted.
    "customer experience associate", "personal banking associate",
    "sales consultant", "banking advisor", "client advisor",
    "universal banker", "mortgage banker", "mortgage specialist",
    "loan processor", "registered client", "registered service associate",
    # Spanish-language retail/branch equivalents (BBVA/Santander/HSBC LatAm/
    # CaixaBank full-board audit, July 2026) — "asesor"/"gestor" alone are too
    # generic (could hit a legit investment advisor), so these are the
    # specific retail-flavored compounds actually observed.
    "asesor universal", "asesor comercial", "asesor de ventas",
    "asesor multicanal", "ejecutivo de cuenta", "ejecutivo negocios",
    "gestor comercial", "gestor de negocios", "gerente de sucursal",
    "administrador de sucursal", "semillero", "generador de talento",
    "cajero",
    # German self-employed/retail-insurance financial-advisor model (Deutsche
    # Bank Finanzberatung network) — distinct from institutional wealth
    # advisory, which doesn't use this title. "selbständig"/"selbstständig"
    # (both spellings) marks the franchise/self-employed model itself — no
    # institutional analyst-rung title contains it.
    "finanzberater", "selbständig", "selbstständig",
}


# Pure engineering / IT / infrastructure. "Data engineer" / "ML engineer"
# are out; "Quantitative Researcher" / "Quant Developer" stay in (LLM will
# judge).
TECH_DROPS = {
    "software engineer", "software developer", "swe ",
    "backend engineer", "frontend engineer", "fullstack engineer",
    "full-stack engineer", "full stack engineer",
    "devops", "site reliability engineer", "sre ",
    "network engineer", "cloud engineer", "infrastructure engineer",
    "platform engineer", "systems engineer",
    "data engineer", "ml engineer", "machine learning engineer",
    "ai engineer",
    "cybersecurity", "cyber security", "information security",
    "application security", "security engineer", "security analyst",
    "security assurance", "security architect", "security operations",
    "appsec", "infosec", "cyber threat",
    "ux researcher",
    "systemadministrator", "system administrator",
    "datenbankadministrator", "database administrator",
    "qa engineer", "qa analyst", "test engineer", "qa automation",
    "web developer", "product owner", "application owner",
}


# Support / non-finance functions.
NON_FINANCE_DROPS = {
    # HR / recruiting / people
    "human resources", "hrbp", "people operations", "people partner",
    "talent acquisition", "recruiter", "recruiting", "talent partner",
    "compensation analyst", "compensation manager",
    # Marketing / comms / PR
    "marketing manager", "marketing analyst", "marketing associate",
    "communications manager", "communications analyst",
    "public relations", "social media",
    "content marketing", "growth marketing",
    "brand manager",
    # Legal / audit / tax
    "legal counsel", "paralegal", "legal analyst",
    "internal audit", "audit analyst", "audit associate", "audit manager",
    "auditor interno", "compliance auditor",
    "tax analyst", "tax accountant",
    # Customer success / SaaS sales / call-center CX
    "customer success", "customer support",
    "customer service representative", "contact center", "contact centre",
    "customer journey",
    "sales development representative", "account executive",
    # Facilities / admin
    "receptionist", "administrative assistant",
    "facilities", "office management", "office manager",
    "executive assistant", "catering",
    "graphic design", "ux designer", "ui designer",
    # Procurement / vendor
    "procurement", "vendor management",
}


# Investment Banking, ECM/DCM, leveraged finance, coverage banking — all
# opted out per the user's profile.
IBD_DROPS = {
    "m&a ", "mergers and acquisitions", "mergers & acquisitions",
    "ecm", "equity capital markets", "dcm", "debt capital markets",
    "leveraged finance", "lev fin",
    "sponsor coverage", "sponsors group",
    "industry coverage", "sector coverage",
    "investment banking analyst", "investment banking associate",
    "ibd ",
    "transaction services", "deal advisory",
    "corporate finance advisory", "boutique m&a",
}


# Internship-shaped roles are out of scope — the user wants full-time
# graduate / junior roles for a May 2027 start. Toggle by editing this set.
INTERNSHIP_MARKERS = {
    # EN
    "intern", "internship", "placement",
    "apprentice", "apprenticeship",
    # DE — Ausbildung (vocational) + dual study
    "praktikum", "praktikant", "praktikantin",
    "werkstudent", "werkstudentin", "working student",
    "ausbildung", "auszubildende", "duales studium", "dual study",
    # FR
    "stage", "stagiaire", "alternance", "alternant", "alternante", "apprenti",
    # IT
    "tirocinio", "stagista",
    # ES
    "becario", "becaria", "prácticas", "practicas",
    # PT — "estagiário/estagiária" (the person form, distinct from estágio)
    "estagio", "estágio", "estagiár",
    # Thesis / academic-only
    "thesis", "bachelor thesis", "master thesis", "abschlussarbeit",
}


# Pre-degree / school-level programmes are ALWAYS dropped (unlike university
# internships/werkstudent/graduate schemes, which the store-broad model keeps
# and the UI filters). the user holds a bachelor's and starts a master's, so
# school-leaver / vocational / apprentice tracks are pure noise. This is a hard
# gate, NOT the strict-only INTERNSHIP_MARKERS set above.
#
# Apprenticeships are handled separately (via _APPRENTICE_RE below) because UK
# *degree* / *graduate* apprenticeships are university-level and stay in scope —
# only non-degree apprenticeships are dropped.
PRE_DEGREE_DROPS = {
    # EN
    "school internship", "school leaver", "school-leaver", "high school",
    "highschool", "secondary school", "sixth form", "work experience programme",
    "work experience placement", "work experience week", "pre-university",
    # DE — Schülerpraktikum (school internship), Ausbildung / Azubi (vocational)
    "schülerpraktikum", "schuelerpraktikum", "schülerpraktika", "berufsausbildung",
    "ausbildung", "auszubildende", "azubi", "duale ausbildung",
    # FR / IT / ES school-level work experience
    "stage de 3ème", "stage de troisième", "stage découverte",
}

# English apprenticeships (UK school-leaver, Brazil "jovem aprendiz", etc.):
# drop unless EXEMPT (see below). These are pre-degree vocational tracks.
_APPRENTICE_RE = re.compile(r"apprentice(?:ship)?", re.IGNORECASE)
# Exemptions — university-level apprentice roles the user wants:
#  - UK "degree"/"graduate"/"master's" apprenticeships, AND
#  - French Bac+5 work-study that gets rendered with the English word
#    "Apprentice" but carries a French tell: an H/F-style gender marker,
#    "alternan(t/ce)", or the French "apprenti(e)" (word-boundary, so it does
#    NOT match the English "apprentice"). Without this, roles like BNP's
#    "Portfolio Manager Apprentice - Alternative Credit H/F" would be dropped.
_APPRENTICE_EXEMPT_RE = re.compile(
    r"(?:degree|graduate|master'?s?|bachelor'?s?)\s+apprentice"
    r"|\b[hfm]\s*/\s*[hfm]\b|\b[hfm]\s*-\s*[hfm]\b"
    r"|alternan|\bapprenti\b|\bapprenti\(e\)",
    re.IGNORECASE,
)


# Exemption from the senior-title drop for cases where a senior-sounding word
# is actually part of a firm or division name embedded in the job title. The
# canonical example: "Analyst, Principal Investments" — "Principal" is the
# asset-management firm, not a seniority level. Pattern: principal immediately
# followed by a noun that reads as an organisation type.
_SENIOR_TITLE_EXEMPT_RE = re.compile(
    r"\bprincipal\s+(?:invest|financial|capital|asset|fund|group|partner)",
    re.IGNORECASE,
)


# Dual-rung postings ("Analyst/Associate", "Analyst or Associate", etc.)
# hire at both levels — the user qualifies at the Analyst rung, so these
# should bypass the "associate" senior trigger.
_DUAL_RUNG_RE = re.compile(
    r"analyst\s*(?:[/,&]|\s+or\s+|\s*-\s*)\s*associate"
    r"|associate\s*(?:[/,&]|\s+or\s+|\s*-\s*)\s*analyst",
    re.IGNORECASE,
)


# Multi-year experience demands in the TITLE signal a senior role even
# without a senior marker. Description-level walls are handled by
# `has_experience_wall` below, called from main.py before scoring.
_YOE_RE = re.compile(
    r"(\d+)\s*\+?\s*(?:years|yrs|jahre|ans|años|anni)",
    re.IGNORECASE,
)

# Range phrasing directly before the matched number: "0-3 years" / "2 to 4
# years" / "1 bis 3 Jahre". _YOE_RE only ever sees the UPPER bound (the number
# adjacent to "years"), which reads an entry-level window as a senior wall —
# judge ranges by their lower bound instead. Mirrors _YOE_EXPLICIT_MIN_RE
# below, which deliberately excludes ranges.
_YOE_RANGE_LOWER_RE = re.compile(
    r"(\d+)\s*(?:[-–—/]|to|bis|à|a)\s*$",
    re.IGNORECASE,
)


def _demands_experience(text: str, threshold: int = 3) -> bool:
    for match in _YOE_RE.finditer(text):
        try:
            years = int(match.group(1))
        except ValueError:
            continue
        lower = _YOE_RANGE_LOWER_RE.search(text[: match.start(1)])
        if lower:
            years = int(lower.group(1))
        if years >= threshold:
            return True
    return False


# --- Description-level YoE wall detection (used by main.py pre-scoring) ---
# Explicit minimum-experience phrasing across EN/DE/FR/ES. Matches "minimum 5
# years", "at least 5 years", "requires 5 years", "mindestens 5 Jahre", "au
# moins 5 ans", "mínimo 5 años". Range phrasings like "0-3 years" deliberately
# don't match — those are entry-level windows, not walls.
_YOE_EXPLICIT_MIN_RE = re.compile(
    r"(?:minimum\s+(?:of\s+)?|at\s+least\s+|plus\s+|over\s+|more\s+than\s+|>=?\s*"
    r"|requires?\s+|mindestens\s+|au\s+moins\s+|m[ií]nimo\s+(?:de\s+)?)"
    r"(\d+)\s*\+?\s*(?:years|yrs|jahre|ans|años|anni)",
    re.IGNORECASE,
)

# "N+ years" — the trailing + is the wall marker.
_YOE_PLUS_RE = re.compile(
    r"(\d+)\+\s*(?:years|yrs|jahre|ans|años|anni)",
    re.IGNORECASE,
)

# Bare range "N-M years" / "N to M years" / "N bis M Jahre" — the LOWER bound is
# the real minimum. Previously has_experience_wall saw none of these (it only
# matched explicit-minimum + N+ phrasings), so InCommodities' "You have 3-5
# years of experience" fell through to min_yoe=0 despite being a hard 3y wall.
# We deliberately capture the LOWER bound: "0-3 years" stays no-wall (0 < 3),
# "3-5 years" walls at 3. Mirrors _YOE_RANGE_LOWER_RE used for title ranges.
_YOE_RANGE_RE = re.compile(
    r"(\d+)\s*(?:[-–—/]|to|bis|à|a)\s*\d+\s*\+?\s*(?:years|yrs|jahre|ans|años|anni)",
    re.IGNORECASE,
)

# "experience" tokens we look for within a small window of the YoE match, to
# avoid false positives on incidental year counts (company age, project
# timelines, etc.).
_EXPERIENCE_NEAR_RE = re.compile(
    r"experience|experienced|exp\.|expérience|erfahrung|jahrige|berufserfahrung",
    re.IGNORECASE,
)

_YOE_CONTEXT_CHARS = 80

# Sentence / clause boundaries. We clamp the proximity window to the clause the
# YoE match sits in so an "experience" token bleeding in from an ADJACENT
# sentence can't manufacture a wall (e.g. "...founded 5 years ago. Prior
# experience is a plus..." is NOT a 5y wall).
_SENTENCE_BREAK_RE = re.compile(r"[.!?;]\s|[\n\r]")


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Bounds of the sentence/clause containing text[start:end], splitting on
    . ! ? ; and newlines. The match itself may span a break (rare), so we anchor
    the left bound to the last break at/before `start` and the right bound to the
    first break at/after `end`."""
    left = 0
    right = len(text)
    for m in _SENTENCE_BREAK_RE.finditer(text):
        if m.end() <= start:
            left = m.end()
        elif m.start() >= end:
            right = m.start()
            break
    return (left, right)


def has_experience_wall(description: str, threshold: int = 3) -> tuple[bool, int]:
    """Returns (True, N) if the description states a minimum experience
    requirement of `threshold`+ years near an "experience" mention. Saved to
    `min_yoe` on every stored row; the web app hides ≥3 yrs by default."""
    if not description:
        return (False, 0)
    candidates: list[tuple[int, int, int]] = []
    for m in _YOE_EXPLICIT_MIN_RE.finditer(description):
        try:
            candidates.append((int(m.group(1)), m.start(), m.end()))
        except ValueError:
            continue
    for m in _YOE_PLUS_RE.finditer(description):
        try:
            candidates.append((int(m.group(1)), m.start(), m.end()))
        except ValueError:
            continue
    # Bare ranges — take the LOWER bound as the minimum ("3-5 years" → 3).
    for m in _YOE_RANGE_RE.finditer(description):
        try:
            candidates.append((int(m.group(1)), m.start(), m.end()))
        except ValueError:
            continue
    for n, start, end in candidates:
        if n < threshold:
            continue
        # Clamp the ±80-char proximity window to the clause the match sits in,
        # so an "experience" token from a neighbouring sentence can't be read as
        # nearby (H2 false positive). The self-contained "minimum N years" /
        # "N+ years" phrasings still need the experience token, but it's almost
        # always in the same clause ("minimum 5 years' experience").
        clause_start, clause_end = _clause_bounds(description, start, end)
        window_start = max(clause_start, start - _YOE_CONTEXT_CHARS)
        window_end = min(clause_end, end + _YOE_CONTEXT_CHARS)
        if _EXPERIENCE_NEAR_RE.search(description[window_start:window_end]):
            return (True, n)
    return (False, 0)


def _contains_term(text: str, term: str) -> bool:
    """Word-boundary match for short / abbreviated terms; right-side boundary
    match for single-word longer terms; substring match for multi-word phrases.

    Short terms (≤3 chars) and dotted abbreviations use a full left+right
    word-boundary regex so 'md' can't hit 'commodity' and 'vp' still fires.

    Single-word terms longer than 3 chars use a RIGHT-side word boundary only
    (term\\b). This prevents false prefix matches — 'lead' no longer hits
    'leadership' or 'leading' — while still catching German compound-word
    suffixes: 'leiter' matches 'teamleiter', 'projektleiter', etc., because
    the boundary fires at the END of the compound, not on the left side.

    Multi-word phrases (contain a space) keep plain substring matching because
    they're already specific enough and don't suffer prefix collisions."""
    if len(term) <= 3 or term.endswith("."):
        if term.endswith(" "):
            # 'sr ' / 'md ': a trailing space in the term means "standalone
            # abbreviation". Keeping the literal space in the pattern demanded
            # a space followed by a NON-word char, which no real title has —
            # 'Sr Analyst' never matched. Treat the space as a boundary
            # instead; '&' stays excluded so 'MD&A Reporting' survives.
            return re.search(
                rf"(?<!\w){re.escape(term.rstrip())}(?![\w&])", text
            ) is not None
        return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None
    if " " not in term:
        # Single-word term: right-side boundary prevents 'lead'→'leadership',
        # 'senior'→'seniority', 'staff'→'staffing', 'director'→'directorate',
        # while 'leiter' still catches 'teamleiter', 'projektleiter', etc.
        return re.search(rf"{re.escape(term)}\b", text, re.IGNORECASE) is not None
    return term in text


def _hits(text: str, bucket: set[str]) -> bool:
    for term in bucket:
        if _contains_term(text, term):
            return True
    return False


def _location_hit(location: str, terms: set[str]) -> bool:
    """Word-boundary match for location drops. Plain substring matching was
    dropping legitimate roles: 'india' matched 'Indiana'/'Indianapolis', so US
    Midwest postings vanished pre-store. Word boundaries keep multi-word terms
    ('new delhi') working while requiring the term to stand alone."""
    for term in terms:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", location):
            return True
    return False


def is_relevant(job: dict, category: str = "", strict: bool | None = None,
                extra_drops: set | None = None) -> bool:
    """Return True if the job should be stored. Negative filter — anything not
    explicitly dropped passes through. `category` is accepted for back-compat
    with main.py but unused.

    Noise-scope (June 2026): the unambiguous-noise buckets — back-office, tech,
    non-finance — are dropped ALWAYS, not just in strict mode. This is what lets
    us point the heavy executor at a giant bank board (pull the full 7k), shed
    the ops/IT/HR bulk by title, and keep every finance division (markets, IBD,
    AM, central-bank, consulting) for the Haiku tagger + web filter to sort. The
    division-level preferences (no wealth / no in-house treasury, yes corporate-
    markets) are toggles on the site, not scrape-time drops — too close to call
    by keyword, and destructive if wrong.

    `extra_drops` is a per-source noise set from targets.json (a firm with its
    own bulk that the global lists don't know — keeps one global list from being
    a single point of failure). `strict` (JOBS_STRICT_FILTER=1) still gates IBD
    + internships for the aggressive scrape mode."""
    if strict is None:
        strict = STRICT_FUNCTION_FILTER
    title = (job.get("title") or "").lower()
    location = (job.get("location") or "").lower()

    # --- Hard gates (always applied) ---

    # Location-based drops first (cheapest, most reductive). Word-boundary
    # matched so 'india' can't swallow 'Indiana'/'Indianapolis'.
    if _location_hit(location, LOCATION_DROPS):
        return False

    # Pre-degree / school-level programmes: ALWAYS dropped (out of scope for a
    # master's student). Degree/graduate apprenticeships are exempted.
    if _hits(title, PRE_DEGREE_DROPS):
        return False
    if _APPRENTICE_RE.search(title) and not _APPRENTICE_EXEMPT_RE.search(title):
        return False

    # Internships: stored in the store-broad default (browsable in the web app,
    # tagged job_type=internship, hidden behind the show_internships toggle) but
    # dropped in strict scrape mode (JOBS_STRICT_FILTER). the user is a student,
    # so internships are in scope for browsing.
    if strict and _hits(title, INTERNSHIP_MARKERS):
        return False

    # Seniority hard gate (always on). "associate" is handled separately below
    # because its rung is firm-dependent.
    dual_rung = bool(_DUAL_RUNG_RE.search(title))
    if _hits(title, SENIOR_TITLE_DROPS) and not _SENIOR_TITLE_EXEMPT_RE.search(title):
        return False

    # "associate" is the SENIOR rung at banks (post-analyst) but the ENTRY rung
    # at PE / asset managers / consulting. Under the store-broad default we keep
    # it (tagged seniority=associate, hidden behind the web app's "Include
    # associate roles" toggle); strict mode still drops it, except dual-rung
    # "Analyst/Associate" titles which are analyst-level.
    if strict and not dual_rung and _contains_term(title, "associate"):
        return False

    # Multi-year experience demand in the title → senior.
    if _demands_experience(title):
        return False

    # --- Unambiguous-noise drops (ALWAYS — never wanted at any source) ---
    if _hits(title, BACK_OFFICE_DROPS):
        return False
    if _hits(title, RETAIL_DROPS):
        return False
    if _hits(title, TECH_DROPS):
        return False
    if _hits(title, NON_FINANCE_DROPS):
        return False
    if extra_drops and _hits(title, extra_drops):
        return False

    # --- IBD: opted back IN by default (June 2026 broaden); strict still drops ---
    if strict and _hits(title, IBD_DROPS):
        return False

    return True
