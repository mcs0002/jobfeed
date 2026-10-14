"""Text a career site serves in place of a job description.

A fetch that returns a bot check, a "description only in French" notice, an
error page or a "position filled" page used to be stored as the description:
the row then counted as enriched, was never retried, and the tagger, the
experience-wall check and Browse search all read the junk. Every write path in
JobDB runs ``is_junk`` first and treats a hit as a failed fetch (his report,
2026-10-14; phrases read from the live DB that day).

Only short bodies are judged: a real posting that quotes one of these phrases
runs into the thousands of characters. A notice followed by the real text
("only available in Chinese" + the Chinese body) is not junk and is not listed.
"""
import re

JUNK_MAX_CHARS = 1500

JUNK_RE = re.compile(
    # Bot checks (Evercore, Rothschild & Co Students: 19 rows).
    r"confirm you.?re a real person|quick check needed"
    r"|verify (?:that )?you are (?:a )?human|checking your browser"
    # A bank's careers site: the body exists only in another language setting (78 rows).
    r"|description of this offer is only available in"
    r"|descriptif de cette offre n.est disponible"
    r"|beschrijving van deze vacature is enkel beschikbaar"
    # Closed or unavailable postings served with a 200.
    r"|this position has been filled"
    r"|can.t view this job because it.s not available"
    r"|currently not open for new applications"
    # Error and search pages (Credit Agricole CIB, ABN AMRO, Point72).
    r"|^\s*erreur\s+-"
    r"|wonach suchen sie\?\s+ihre suchanfrage"
    r"|p72 careers - header"
    # Maintenance banners, as scrapers/enrich/descriptions.OUTAGE_PHRASES_RE.
    r"|the system is currently undergoing|currently undergoing maintenance"
    r"|down for maintenance|temporarily unavailable|service unavailable",
    re.IGNORECASE,
)


def is_junk(text: str | None) -> bool:
    """True if `text` is a placeholder page rather than a job description."""
    return bool(text) and len(text) < JUNK_MAX_CHARS and bool(JUNK_RE.search(text))
