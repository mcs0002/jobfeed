"""Attribute global-filter decisions to the rule and term that fired.

  python scripts/filter_audit.py stored [--days N]
      Re-judge stored rows with the CURRENT filter. Rows the filter would now
      reject, grouped by rule/term and by their stored tag, show which terms
      would cost finance roles (the tagger's area is the ground truth).
  python scripts/filter_audit.py scrape OUT.jsonl [--names a,b] [--workers N]
      Scrape the verified sources without storing anything and write every
      posting with its verdict (rule/term or "kept"), so the roles the filter
      throws away can be read. Read-only against jobs.db; never run it while
      a scheduled scan is running (shared rate-limited boards).
  python scripts/filter_audit.py report OUT.jsonl
      Summarise a scrape file: drops per rule/term with sample titles, and
      dropped titles that look like finance work.
"""
import collections
import json
import re
import sqlite3
import sys

sys.path.insert(0, ".")
from jobfeed import filter as F  # noqa: E402
from jobfeed.db import ai_title  # noqa: E402


def _first(text, bucket):
    for term in sorted(bucket):
        if F._contains_term(text, term):
            return term
    return None


def verdict(job: dict, extra=None) -> tuple[str, str]:
    """Mirror of filter.is_relevant (non-strict), returning (rule, term)."""
    title = F._POSTING_PREFIX_RE.sub("", (job.get("title") or "").lower())
    location = (job.get("location") or "").lower()
    for term in sorted(F.LOCATION_DROPS):
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", location):
            return "location", term
    t = _first(title, F.PRE_DEGREE_DROPS)
    if t:
        return "pre_degree", t
    if F._APPRENTICE_RE.search(title) and not F._APPRENTICE_EXEMPT_RE.search(title):
        return "pre_degree", "apprentice"
    t = _first(F._NOT_SENIOR_RE.sub(" ", title), F.SENIOR_TITLE_DROPS)
    if t and not F._SENIOR_TITLE_EXEMPT_RE.search(title) and not F._ENTRY_PROGRAMME_RE.search(title):
        return "senior", t
    if F._demands_experience(title):
        return "yoe_title", "N years"
    t = _first(title, F.BACK_OFFICE_DROPS)
    if t and not F._BACK_OFFICE_EXEMPT_RE.search(title):
        return "back_office", t
    t = _first(title, F.RETAIL_DROPS)
    if t:
        return "retail", t
    m = F._RETAIL_WEALTH_RE.search(title)
    if m:
        return "retail", m.group(0).strip()
    t = _first(title, F.TECH_DROPS)
    if t and not ai_title(job.get("title")) and not F._FINANCE_TECH_RE.search(title):
        return "tech", t
    t = _first(title, F.NON_FINANCE_DROPS)
    if t:
        return "non_finance", t
    if extra:
        t = _first(title, set(extra))
        if t:
            return "source_noise", t
    return "kept", ""


# Finance-looking words, used only to surface dropped titles worth reading.
FIN_RE = re.compile(
    r"\b(trad(?:er|ing)|quant\w*|structur\w*|portfolio|invest\w*|fund manager|"
    r"asset manag\w*|risk|credit|equity|fixed income|rates|fx|derivativ\w*|"
    r"capital markets|m&a|valuation|research analyst|economist|treasury|"
    r"private (?:equity|markets|debt|bank\w*)|wealth|origination|syndicat\w*|"
    r"sales ?&? ?trading|analyst|graduate|trainee|intern\w*)\b", re.I)


def stored(days=None):
    db = sqlite3.connect("jobs.db")
    targets = {t["name"]: t for t in json.load(open("targets.json"))}
    q = "select company, title, location, area from seen_jobs"
    if days:
        q += f" where first_seen > datetime('now', '-{int(days)} days')"
    agg = collections.defaultdict(lambda: collections.Counter())
    samples = collections.defaultdict(list)
    n = 0
    for c, ti, loc, area in db.execute(q):
        n += 1
        rule, term = verdict({"title": ti, "location": loc})
        if rule == "kept":
            continue
        fin = area not in (None, "", "other")
        agg[(rule, term)]["fin" if fin else ("other" if area == "other" else "untagged")] += 1
        if fin and len(samples[(rule, term)]) < 6:
            samples[(rule, term)].append(f"{area}: {ti[:70]} ({c})")
    print(f"{n} stored rows re-judged; rows the current filter would reject:")
    for (rule, term), cnt in sorted(agg.items(), key=lambda kv: -kv[1]["fin"]):
        print(f"  {rule:12s} {term!r:28s} finance={cnt['fin']:4d} other={cnt['other']:4d}")
        for s in samples[(rule, term)]:
            print(f"        + {s}")


def scrape(out, names=None, workers=6):
    from jobfeed.main import scrape_all
    targets = [t for t in json.load(open("targets.json"))
               if t.get("verified") and t.get("ats") not in ("manual", "unknown")
               and (not names or t["name"] in names)]
    with open(out, "w") as fh:
        for company, jobs, err in scrape_all(targets, workers=workers):
            for j in jobs:
                rule, term = verdict(j, company.get("noise_terms"))
                fh.write(json.dumps({"company": company["name"], "category": company.get("category"),
                                     "title": j.get("title"), "location": j.get("location"),
                                     "rule": rule, "term": term}, ensure_ascii=False) + "\n")
            if err:
                fh.write(json.dumps({"company": company["name"], "error": err}) + "\n")


def report(path):
    rows = [json.loads(l) for l in open(path)]
    rows = [r for r in rows if "rule" in r]
    by = collections.Counter(r["rule"] for r in rows)
    print(f"{len(rows)} postings: " + ", ".join(f"{k} {v}" for k, v in by.most_common()))
    terms = collections.defaultdict(list)
    for r in rows:
        if r["rule"] != "kept":
            terms[(r["rule"], r["term"])].append(r)
    for (rule, term), rs in sorted(terms.items(), key=lambda kv: -len(kv[1])):
        fin = [r for r in rs if FIN_RE.search(r["title"] or "")]
        print(f"\n[{rule}] {term!r}: {len(rs)} dropped, {len(fin)} finance-looking")
        for r in fin[:12]:
            print(f"   ? {r['title'][:85]} | {r['company']} | {(r['location'] or '')[:30]}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[0] == "stored":
        stored(int(a[a.index("--days") + 1]) if "--days" in a else None)
    elif a[0] == "scrape":
        names = set(a[a.index("--names") + 1].split(",")) if "--names" in a else None
        workers = int(a[a.index("--workers") + 1]) if "--workers" in a else 6
        scrape(a[1], names, workers)
    elif a[0] == "report":
        report(a[1])
    elif a[0] == "rejudge":
        # Re-apply the CURRENT filter to a scrape file written by an older one.
        moved = collections.Counter()
        for line in open(a[1]):
            r = json.loads(line)
            if "rule" not in r:
                continue
            new = verdict(r)
            new = new if new[0] != "kept" or r["rule"] != "source_noise" else (r["rule"], r["term"])
            if (r["rule"] == "kept") != (new[0] == "kept"):
                key = ("now kept" if new[0] == "kept" else "now dropped",
                       r["rule"] if new[0] == "kept" else new[0], r["term"] if new[0] == "kept" else new[1])
                moved[key] += 1
                if moved[key] <= 6:
                    print(f"  {key}: {r['title'][:80]} | {r['company']}")
        for k, n in moved.most_common():
            print(n, k)
