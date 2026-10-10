"""Suggest per-source noise terms: tokens frequent in a source's 'other' titles
and absent from its finance titles (all rows ever stored, incl. delisted).
usage: python scripts/noise_terms.py "Company" [min_count] [--test term1,term2,...]"""
import collections, re, sqlite3, sys
sys.path.insert(0, ".")
from jobfeed.filter import _contains_term
name = sys.argv[1]
mn = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 4
db = sqlite3.connect("jobs.db")
rows = db.execute("select title, area, delisted_at is null from seen_jobs where company=? and area is not null and area!=''", (name,)).fetchall()
if "--test" in sys.argv:
    terms = sys.argv[sys.argv.index("--test") + 1].split(",")
    hit_o = hit_f = 0
    for t, a, live in rows:
        tl = t.lower()
        if any(_contains_term(tl, x) for x in terms):
            if a == "other": hit_o += 1
            else:
                hit_f += 1; print(f"   FIN HIT {a}: {t[:90]}")
    act = [r for r in rows if r[2]]
    ao = sum(1 for t, a, l in act if a == "other" and any(_contains_term(t.lower(), x) for x in terms))
    print(f"{name}: terms hit other={hit_o} finance={hit_f} (all-time); active other hit {ao} of {sum(1 for r in act if r[1]=='other')}")
    sys.exit()
tok = lambda t: set(re.findall(r"[a-zà-ÿ][a-zà-ÿ&+.-]{2,}", t.lower()))
co, cf = collections.Counter(), collections.Counter()
for t, a, _ in rows:
    (co if a == "other" else cf).update(tok(t))
print(f"{name}: {sum(1 for r in rows if r[1]=='other')} other, {sum(1 for r in rows if r[1]!='other')} finance (all-time)")
print(", ".join(f"{w}:{n}" for w, n in co.most_common(400) if n >= mn and cf[w] == 0))
