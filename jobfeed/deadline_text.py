"""Closing dates a posting states in its own words.

Many boards publish no machine-readable deadline (Greenhouse, Lever, most
Workday bodies) yet the description says it outright: "Applications will
close on Friday 30 October 2026", "Application Deadline: 07/22/2026",
"Bewerbungsschluss: 15.10.2026". This reads that sentence, and nothing else.

The same doctrine as application limits: a date is only ever copied from
text we hold, never inferred. So the date must sit right after a deadline
label, in the same sentence; the year must be written; a numeric date is
accepted only when it cannot be read two ways (07/22/2026 yes, 05/06/2026
no); and the result passes the same future / within-a-year gate as
schema.org validThrough (scrapers/enrich/descriptions.py), because the web
app hides a role whose deadline has passed and a wrong past date would hide
a live role. "Meet deadlines" boilerplate never matches: every label is a
deadline-for-applications phrase.

    python -m jobfeed.deadline_text            # dry run over stored rows
    python -m jobfeed.deadline_text --apply    # write, with an undo CSV
"""
from __future__ import annotations

import argparse
import csv
import os
import re
from datetime import date, datetime, timedelta

# Same horizon as schema.org validThrough: further out is SEO filler.
from scrapers.enrich.descriptions import MAX_DEADLINE_DAYS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Label → the date must follow within this many characters, same sentence.
_WINDOW = 70

_LABELS = [
    r"application\s+deadline", r"deadline\s+for\s+(?:all\s+)?applications?",
    r"applications?\s+deadline", r"closing\s+date(?:\s+for\s+applications?)?",
    r"applications?\s+(?:will\s+)?close(?:s|d)?", r"apply\s+(?:no\s+later\s+than|by|before)",
    r"applications?\s+(?:must|should)\s+be\s+(?:received|submitted)\s+(?:by|before|no\s+later\s+than)",
    # "please submit it before the end of the day on (dd.mm.yyyy): 07.10.2026"
    # (Equinor, 2026-09-24): the format hint sits between label and date.
    r"submit\s+(?:it|this|your\s+application|your\s+cv|applications?)\s+"
    r"(?:by|before|no\s+later\s+than)(?:\s+the\s+end\s+of\s+(?:the\s+)?day)?(?:\s+on)?",
    # Added 2026-09-24 from jobfeed.deadline_llm_check's verified finds on the
    # live DB, each a phrasing these rules missed:
    # "Posting End Date: 30/09/2026" (Standard Chartered), the date Workday
    # and Oracle publish as a field, here written into the body.
    r"(?:job\s+)?posting\s+(?:end|close|closing)\s+date",
    # "This position will be open through October 11, 2026." (Janus Henderson)
    r"open\s+(?:through|until|till)",
    # "Applications open September 9, 2026, and close November 15, 2026" (Evercore)
    r"applications?\s+open\b[^.;\n]{0,60}?\band\s+close[sd]?(?:\s+on)?",
    # "send in your application today, but no later than4th of October" (SEB)
    r"application[^.;\n]{0,40}?\bno\s+later\s+than",
    # Second round, same source (2026-09-24 rerun):
    # "Recruiting for this role ends on 12/31/2026." (Deloitte)
    r"recruiting\s+for\s+this\s+(?:role|position)\s+ends(?:\s+on)?",
    # Third round: "Application expected to close: 12/23/2026" (Geneva Trading)
    r"applications?\s+(?:is\s+|are\s+)?(?:expected|scheduled|due)\s+to\s+close(?:\s+on)?",
]

# Labels written in languages whose numeric dates are day-first. A slash date
# after one of these is read DD/MM ("Indsend din ansøgning senest 08/10/2026",
# Nordea, is 8 October); after an English label it stays refused unless one
# side cannot be a month, because US postings write MM/DD.
_DAY_FIRST_LABELS = [
    r"bewerbungsschluss", r"bewerbungsfrist", r"bewerben\s+sie\s+sich\s+(?:bitte\s+)?bis(?:\s+zum)?",
    r"date\s+limite(?:\s+de\s+(?:candidature|dépôt|depot))?",
    r"candidatures?\s+(?:jusqu'au|avant\s+le)",
    # Spanish: "Fecha límite para apuntarse: 2026-11-10" (BBVA), plus the
    # other common forms on the Spanish / Latin American boards.
    r"fecha\s+l[íi]mite(?:\s+(?:para\s+(?:apuntarse|postular(?:se)?|aplicar|inscribirse)|"
    r"de\s+(?:postulaci[óo]n|inscripci[óo]n|solicitud|aplicaci[óo]n)))?",
    r"plazo\s+de\s+(?:postulaci[óo]n|inscripci[óo]n|solicitud)",
    # Swedish / Norwegian / Danish: "din ansökan senast den 2026-09-30"
    # (Swedbank), "Indsend din ansøgning senest 08/10/2026" (Nordea),
    # "Søknadsfrist:", "Ansøgningsfrist:", "sista ansökningsdag".
    r"ans[öo]kan[^.;\n]{0,30}?\bsenast(?:\s+den)?",
    r"(?:ans[øo]gning|s[øo]knad)(?:en)?[^.;\n]{0,30}?\bsenest(?:\s+den)?",
    r"sista\s+ans[öo]kningsdag(?:en)?", r"s[øo]knadsfrist", r"ans[øo]gningsfrist",
    # Dutch and Italian.
    r"sluitingsdatum", r"reageren\s+kan\s+tot(?:\s+en\s+met)?",
    r"scadenza(?:\s+(?:candidature|delle\s+candidature))?",
]
# A label may run straight into its date ("no later than4th"), so it ends at
# any non-letter rather than at a word boundary.
_LABEL_RE = re.compile(r"\b(?:" + "|".join(_LABELS + _DAY_FIRST_LABELS) + r")(?![^\W\d_])", re.I)
_DAY_FIRST_RE = re.compile(r"(?:" + "|".join(_DAY_FIRST_LABELS) + r")", re.I)

_MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    # German
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5,
    "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
    # French
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "décembre": 12, "decembre": 12,
    # Spanish, Swedish/Norwegian/Danish, Dutch, Italian (only the spellings
    # not already above)
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    "januari": 1, "februari": 2, "maart": 3, "mars": 3, "maj": 5, "mei": 5,
    "juni": 6, "augusti": 8, "augustus": 8, "oktober": 10, "desember": 12,
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "settembre": 9, "ottobre": 10, "dicembre": 12,
}
_MON = "(?P<mon>" + "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th|er|\.)?"
_YEAR = r"(?P<year>20\d{2})"
_DATE_RES = [
    re.compile(rf"(?<![^\W\d_]){_DAY}(?:\s+(?:of\s+|de\s+)?|-){_MON},?[\s-]+(?:de\s+)?{_YEAR}\b", re.I),  # 30 October 2026, 25-Sep-2026, 15 de octubre de 2026
    re.compile(rf"\b{_MON}[\s-]+{_DAY},?[\s-]+{_YEAR}\b", re.I),        # October 30, 2026, November-30-2026
    re.compile(r"\b(?P<year>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})\b"),        # 2026-10-30
    re.compile(r"\b(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<year>20\d{2})\b"),  # 30.10.2026
    re.compile(r"\b(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<year>20\d{2})\b"),    # 07/22/2026
]
# A full stop after a one- or two-digit number is a German ordinal
# ("1. Dezember"), not a sentence end; after a four-digit year it is one.
SENTENCE_END = re.compile(r"(?:(?<!\d)|(?<=\d{4}))[.!?;]\s|\n")


# After a label crossed a line break, only a weekday may precede the date.
_WEEKDAY_ONLY = re.compile(
    r"\s*(?:(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\.?|"
    r"montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|"
    r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)?[\s,]*", re.I)


def _to_date(m: re.Match, day_first: bool = False) -> date | None:
    g = m.groupdict()
    year = int(g["year"])
    try:
        if g.get("mon"):
            return date(year, _MONTHS[g["mon"].lower()], int(g["day"]))
        if g.get("d") is not None:
            return date(year, int(g["m"]), int(g["d"]))
        a, b = int(g["a"]), int(g["b"])
        # Slash dates are read only when one side cannot be a month.
        if a > 12 >= b:
            return date(year, b, a)      # 22/07/2026
        if b > 12 >= a:
            return date(year, a, b)      # 07/22/2026
        if day_first:
            return date(year, b, a)      # 08/10/2026 after a Danish label
        return None                      # 05/06/2026: two readings, none taken
    except ValueError:
        return None


def stated_deadline(text: str, today: date | None = None) -> tuple[str, str]:
    """(YYYY-MM-DD, the sentence it came from), or ("", "")."""
    if not text:
        return "", ""
    today = today or date.today()
    for label in _LABEL_RE.finditer(text):
        day_first = bool(_DAY_FIRST_RE.fullmatch(label.group(0)))
        tail = text[label.end():label.end() + _WINDOW]
        # A label on its own line ("Application Deadline:" then the date on
        # the next line, RBC 2026-09-24) may cross ONE line break, but then
        # the date has to open that line: "Application Deadline\nStart date:
        # 1 October 2026" must not read the start date as the deadline.
        # Blank lines and non-breaking spaces count as the same break: RBC's
        # "Application Deadline:" still missed its date after the first fix.
        lead = re.match(r"[ \t\u00a0:\u2013\u2014-]*(\n[\s\u00a0]*)?", tail)
        crossed = bool(lead.group(1))
        tail = tail[lead.end():]
        stop = SENTENCE_END.search(tail)
        if stop:
            tail = tail[:stop.start()]
        best = None
        for rx in _DATE_RES:
            m = rx.search(tail)
            if m and (best is None or m.start() < best[0].start()):
                best = (m, _to_date(m, day_first))
        if not best or best[1] is None:
            continue
        if crossed and not _WEEKDAY_ONLY.fullmatch(tail[:best[0].start()]):
            continue
        stated = best[1]
        if today <= stated <= today + timedelta(days=MAX_DEADLINE_DAYS):
            dot = text.rfind(". ", 0, label.start())
            start = max(dot + 2 if dot >= 0 else 0,
                        text.rfind("\n", 0, label.start()) + 1)
            quote = text[start:label.end() + lead.end() + best[0].end()].strip()
            return stated.isoformat(), re.sub(r"\s+", " ", quote)[:240]
    return "", ""


def main() -> None:
    from jobfeed.db import JobDB
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the dates (default: dry run)")
    ap.add_argument("--db", default=os.path.join(ROOT, "jobs.db"))
    args = ap.parse_args()
    db = JobDB(args.db)
    rows = db.conn.execute(
        "SELECT id, company, title, description FROM jobs_with_description "
        "WHERE deadline IS NULL AND delisted_at IS NULL "
        "AND description IS NOT NULL AND description <> ''").fetchall()
    found = []
    for jid, company, title, desc in rows:
        when, quote = stated_deadline(desc)
        if when:
            found.append((jid, company, title, when, quote))
    for jid, company, title, when, quote in found[:40]:
        print(f"{when}  {company[:28]:28}  {title[:50]:50}  | {quote[:90]}")
    print(f"\n{len(found)} of {len(rows)} undated live rows state a closing date.")
    if not args.apply:
        print("Dry run: nothing written. --apply to store them.")
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Beside the DB it changed, like scripts/reapply_guards.py.
    undo = os.path.join(os.path.dirname(os.path.abspath(args.db)),
                        f"deadline_text_undo_{stamp}.csv")
    with open(undo, "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["id", "deadline_before", "deadline_after", "quote"])
        for jid, _c, _t, when, quote in found:
            w.writerow([jid, "", when, quote])
    n = sum(db.set_deadline(jid, when) for jid, _c, _t, when, _q in found)
    print(f"Wrote {n} deadlines. Undo: set deadline back to NULL for the ids in {undo}")


if __name__ == "__main__":
    main()
