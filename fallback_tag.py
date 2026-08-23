"""Deterministic, offline tags for installations without an LLM provider.

The model-backed tagger remains the higher-quality production path. This
fallback deliberately focuses on the fields that make the browse UI useful:
area, desk, seniority, employment type, city, country, region, work mode and
obvious start/education hints. Unknown values stay conservative.
"""
from __future__ import annotations

import re

from tag import (_canon_city, _canon_country, _coerce_start_date,
                 _enforce_internship, _enforce_manager)


PROVENANCE = {
    "provider": "local",
    "model": "deterministic-fallback-v1",
    "rubric_version": "fallback-v1",
}


_COUNTRY_REGION = {
    # Europe
    "Austria": "Europe", "Belgium": "Europe", "Czech Republic": "Europe",
    "Denmark": "Europe", "Finland": "Europe", "France": "Europe",
    "Germany": "Europe", "Greece": "Europe", "Hungary": "Europe",
    "Ireland": "Europe", "Italy": "Europe", "Luxembourg": "Europe",
    "Netherlands": "Europe", "Norway": "Europe", "Poland": "Europe",
    "Portugal": "Europe", "Romania": "Europe", "Spain": "Europe",
    "Sweden": "Europe", "Switzerland": "Europe", "United Kingdom": "Europe",
    # Americas
    "Argentina": "Americas", "Brazil": "Americas", "Canada": "Americas",
    "Chile": "Americas", "Colombia": "Americas", "Mexico": "Americas",
    "United States": "Americas",
    # APAC
    "Australia": "APAC", "China": "APAC", "Hong Kong": "APAC",
    "India": "APAC", "Indonesia": "APAC", "Japan": "APAC",
    "Malaysia": "APAC", "New Zealand": "APAC", "Philippines": "APAC",
    "Singapore": "APAC", "South Korea": "APAC", "Taiwan": "APAC",
    "Thailand": "APAC", "Vietnam": "APAC",
    # Middle East / Africa
    "Bahrain": "MEA", "Egypt": "MEA", "Israel": "MEA", "Kenya": "MEA",
    "Nigeria": "MEA", "Qatar": "MEA", "Saudi Arabia": "MEA",
    "South Africa": "MEA", "United Arab Emirates": "MEA",
}

_COUNTRY_ALIASES = {
    "czechia": "Czech Republic", "czech republic": "Czech Republic",
    "korea": "South Korea", "south korea": "South Korea",
    "taiwan": "Taiwan", "new zealand": "New Zealand",
    "south africa": "South Africa", "israel": "Israel",
    "denmark": "Denmark", "norway": "Norway", "finland": "Finland",
    "greece": "Greece", "hungary": "Hungary", "romania": "Romania",
    "argentina": "Argentina", "chile": "Chile", "colombia": "Colombia",
    "indonesia": "Indonesia", "malaysia": "Malaysia",
    "philippines": "Philippines", "thailand": "Thailand", "vietnam": "Vietnam",
    "bahrain": "Bahrain", "egypt": "Egypt", "kenya": "Kenya",
    "nigeria": "Nigeria", "south africa": "South Africa",
}

_CITY_COUNTRY = {
    "amsterdam": "Netherlands", "rotterdam": "Netherlands",
    "london": "United Kingdom", "edinburgh": "United Kingdom",
    "frankfurt": "Germany", "frankfurt am main": "Germany",
    "berlin": "Germany", "munich": "Germany", "münchen": "Germany",
    "paris": "France", "milan": "Italy", "madrid": "Spain",
    "lisbon": "Portugal", "dublin": "Ireland", "luxembourg": "Luxembourg",
    "zurich": "Switzerland", "zürich": "Switzerland", "geneva": "Switzerland",
    "vienna": "Austria", "brussels": "Belgium", "stockholm": "Sweden",
    "copenhagen": "Denmark", "oslo": "Norway", "helsinki": "Finland",
    "warsaw": "Poland", "prague": "Czech Republic", "budapest": "Hungary",
    "bucharest": "Romania", "athens": "Greece",
    "new york": "United States", "chicago": "United States",
    "boston": "United States", "austin": "United States",
    "miami": "United States", "philadelphia": "United States",
    "san francisco": "United States", "los angeles": "United States",
    "washington": "United States", "toronto": "Canada", "montreal": "Canada",
    "vancouver": "Canada", "mexico city": "Mexico", "são paulo": "Brazil",
    "sao paulo": "Brazil", "santiago": "Chile", "bogota": "Colombia",
    "sydney": "Australia", "melbourne": "Australia", "singapore": "Singapore",
    "hong kong": "Hong Kong", "shanghai": "China", "beijing": "China",
    "shenzhen": "China", "tokyo": "Japan", "osaka": "Japan",
    "taipei": "Taiwan", "seoul": "South Korea", "mumbai": "India",
    "bangalore": "India", "bengaluru": "India", "new delhi": "India",
    "dubai": "United Arab Emirates", "abu dhabi": "United Arab Emirates",
    "doha": "Qatar", "riyadh": "Saudi Arabia", "tel aviv": "Israel",
    "johannesburg": "South Africa", "cape town": "South Africa",
}

_US_STATE_RE = re.compile(
    r"\b(?:alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
    r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|"
    r"utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|dc)\b",
    re.IGNORECASE,
)


def _canonical_country(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    canonical = _canon_country(raw)
    return _COUNTRY_ALIASES.get(canonical.casefold(), canonical)


def parse_location(location: str) -> tuple[str, str, str, str]:
    """Return (city, country, region, work_mode) from an ATS location label."""
    raw = (location or "").strip()
    low = raw.casefold()
    work_mode = ("remote" if re.search(r"\bremote\b|home[- ]?office", low)
                 else "hybrid" if re.search(r"\bhybrid\b", low)
                 else "")
    # Multi-location boards commonly use semicolons. The first advertised
    # location is the least surprising facet value and matches tag._canon_country.
    first = re.split(r"[;|/]", raw, maxsplit=1)[0].strip()
    parts = [re.sub(r"\s*\([^)]*\)\s*", "", p).strip()
             for p in first.split(",") if p.strip()]
    parts = [p for p in parts if p.casefold() not in {"remote", "hybrid", "onsite"}]

    country = ""
    for part in reversed(parts):
        candidate = _canonical_country(part)
        if candidate in _COUNTRY_REGION:
            country = candidate
            break
    if not country and _US_STATE_RE.search(first):
        country = "United States"
    if not country:
        for part in parts:
            if part.casefold() in _CITY_COUNTRY:
                country = _CITY_COUNTRY[part.casefold()]
                break

    city = ""
    if parts:
        first_part = parts[0]
        first_country = _canonical_country(first_part)
        if first_country not in _COUNTRY_REGION:
            city = _canon_city(first_part)
        elif len(parts) == 1 and first_country in {"Singapore", "Hong Kong"}:
            city = first_country

    return city, country, _COUNTRY_REGION.get(country, ""), work_mode


_AREA_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("private-equity", re.compile(r"\bprivate equity\b|\bbuyouts?\b|\bgrowth equity\b", re.I)),
    ("debt", re.compile(r"\bprivate credit\b|\bdirect lending\b|\bcredit investments?\b", re.I)),
    ("wealth", re.compile(r"\bwealth\b|\bprivate bank(?:er|ing)?\b", re.I)),
    ("ibd", re.compile(r"\binvestment bank(?:er|ing)?\b|\bm\s*&\s*a\b|mergers?\s*(?:and|&)\s*acquisitions?|\bleveraged finance\b|\bcorporate finance advisory\b", re.I)),
    ("capital-markets", re.compile(r"\bcapital markets?\b|\b(?:dcm|ecm)\b|\bdebt capital\b|\bequity capital\b|\bsyndicate\b", re.I)),
    ("corporate-banking", re.compile(r"\bcorporate bank(?:er|ing)?\b|\bcommercial bank(?:er|ing)?\b|\btransaction banking\b|\btrade finance\b|\bcash management\b", re.I)),
    ("quant", re.compile(r"\bquant(?:itative)?\b|\bsystematic\b|\balgorithmic trad(?:er|ing)\b", re.I)),
    ("research", re.compile(r"\bequity research\b|\bcredit research\b|\binvestment research\b|\bresearch analyst\b|\beconomist\b|\bmacro strategist\b", re.I)),
    ("markets", re.compile(r"\btrad(?:e|er|ers|ing)\b|\bmarket mak(?:er|ing)\b|\bstructur(?:er|ing)\b|\bderivatives?\b|\bfixed income\b|\btreasur(?:y|ies)\b|\bforeign exchange\b|\bfx\b|\bcommodit(?:y|ies)\b|\binstitutional sales\b|\bequity sales\b", re.I)),
    ("risk", re.compile(r"\brisk\b", re.I)),
    ("actuarial", re.compile(r"\bactu(?:ary|arial)\b", re.I)),
    ("consulting", re.compile(r"\bconsult(?:ant|ing)\b", re.I)),
    ("accounting", re.compile(r"\baccount(?:ant|ing)\b|\baudit(?:or|ing)?\b|\btax\b|\bfinancial reporting\b", re.I)),
    ("asset-management", re.compile(r"\basset management\b|\bportfolio (?:manager|analyst)\b|\binvestment (?:manager|analyst)\b|\bfund manager\b", re.I)),
    ("middle-office", re.compile(r"\bmiddle office\b|\bcompliance\b|\bsettlements?\b|\btrade support\b", re.I)),
]

_SUPPORT_RE = re.compile(
    r"\bsoftware\b|\bdeveloper\b|\bengineer\b|\binfrastructure\b|\bdata center\b|"
    r"\bhuman resources?\b|\brecruit(?:er|ing|ment)\b|\bmarketing\b|\bcommunications?\b|"
    r"\blegal\b|\bprocurement\b|\bsupply chain\b|\bcyber\b|\binformation technology\b|\bit support\b",
    re.I,
)


def classify_area(title: str, category: str = "") -> str:
    text = title or ""
    for area, pattern in _AREA_RULES:
        if pattern.search(text):
            return area

    category_low = (category or "").casefold()
    if _SUPPORT_RE.search(text):
        return "other"
    if "prop trading" in category_low or "market maker" in category_low:
        return "markets"
    if any(term in category_low for term in
           ("asset manager", "hedge fund", "pension", "sovereign wealth")):
        if re.search(r"\binvestment\b|\bportfolio\b|\bfund\b|\banalyst\b", text, re.I):
            return "asset-management"
    if "consult" in category_low or "advisory" in category_low:
        return "consulting"
    return "other"


def fallback_tag(job: dict) -> dict:
    """Add conservative offline facets to one scraped job in place."""
    title = job.get("title") or ""
    area = classify_area(title, job.get("category") or "")
    desk = ""
    if area == "markets":
        if re.search(r"\bsales\b", title, re.I):
            desk = "sales"
        elif re.search(r"structur|originat", title, re.I):
            desk = "structuring"
        elif re.search(r"strateg|desk quant", title, re.I):
            desk = "strats"
        else:
            desk = "trading"

    seniority = ""
    job_type = "job"
    if re.search(r"\bgraduate (?:program|programme|scheme)\b|\btrainee program", title, re.I):
        seniority, job_type = "graduate", "graduate-programme"
    elif re.search(r"\bgraduate\b|\bentry[- ]level\b|\bjunior\b|\btrainee\b", title, re.I):
        seniority = "graduate"
    elif re.search(r"\banalyst\b", title, re.I):
        seniority = "analyst"
    elif re.search(r"\bassociate\b", title, re.I):
        seniority = "associate"

    city, country, region, work_mode = parse_location(job.get("location") or "")
    if not work_mode:
        combined = f"{title} {job.get('description') or ''}"[:2500]
        work_mode = ("remote" if re.search(r"\bremote\b|home[- ]?office", combined, re.I)
                     else "hybrid" if re.search(r"\bhybrid\b", combined, re.I)
                     else "")

    year_match = re.search(r"\b(20\d{2})\b", title)
    start_date = _coerce_start_date(year_match.group(1)) if year_match else ""
    education = ("phd" if re.search(r"\bph\.?d\b|doctorate", title, re.I)
                 else "master" if re.search(r"\bmaster'?s?\b|\bm\.sc\.?\b", title, re.I)
                 else "bachelor" if re.search(r"\bbachelor'?s?\b|\bb\.sc\.?\b", title, re.I)
                 else "")

    job.update(
        area=area, desk=desk, seniority=seniority, job_type=job_type,
        loc_city=city, loc_country=country, loc_region=region,
        work_mode=work_mode, lang_req="", education=education,
        start_date=start_date, min_yoe=None,
    )
    _enforce_internship(job)
    _enforce_manager(job)
    return job


def fallback_tag_jobs(jobs: list[dict]) -> list[dict]:
    for job in jobs:
        fallback_tag(job)
    return jobs
