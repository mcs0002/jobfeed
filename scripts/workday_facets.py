"""Map a Workday source's live board onto its facets and stored tags.

usage: python scripts/workday_facets.py "Target name" [facetParam ...] [--show-other]
Prints, per facet value: jobs on board, how many are stored, finance-tagged
(area not other), other-tagged; plus the finance titles, so a facet set can be
chosen that keeps every finance role.
"""
import concurrent.futures as cf
import json
import sqlite3
import sys

sys.path.insert(0, ".")
from scrapers._http import make_session
from scrapers.workday import HEADERS

args = [a for a in sys.argv[1:] if not a.startswith("--")]
SHOW_OTHER = "--show-other" in sys.argv
name, params = args[0], args[1:]
t = json.load(open("targets.json"))
t = t if isinstance(t, list) else t.get("targets", t)
tgt = next(x for x in t if x.get("name") == name)
wd = tgt["workday"]
tenant, board = wd["tenant"], wd["board"]
host = wd.get("host") or f"{tenant}.{wd.get('version', 'wd1')}.myworkdayjobs.com"
api = f"https://{host}/wday/cxs/{tenant}/{board}/jobs"
s = make_session()


def post(facets, offset=0, limit=20, text=""):
    r = s.post(api, json={"appliedFacets": facets, "limit": limit, "offset": offset,
                          "searchText": text}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def ids_for(facets, text=""):
    first = post(facets, text=text)
    total = int(first.get("total", 0))
    pages = [first]
    with cf.ThreadPoolExecutor(6) as ex:
        pages += list(ex.map(lambda o: post(facets, o, text=text), range(20, total, 20)))
    out = {}
    for p in pages:
        for j in p.get("jobPostings", []):
            path = j.get("externalPath", "")
            if path:
                out[f"wd_{tenant}_{path.split('/')[-1]}"] = j.get("title", "")
    return total, out


def flat(facets, prefix=""):
    for f in facets:
        vals = f.get("values", [])
        if vals and "facetParameter" in vals[0]:
            yield from flat(vals, prefix + f.get("facetParameter", "") + ">")
        else:
            yield prefix + f.get("facetParameter", ""), f.get("descriptor", ""), vals


first = post({}, limit=1)
print(f"{name}: tenant={tenant} board={board} full board total={first.get('total')}")
print("configured:", json.dumps({k: wd[k] for k in ("applied_facets", "search_text") if k in wd}))
facets = list(flat(first.get("facets", [])))
for p, d, vals in facets:
    print(f"  facet {p} ({d}): {len(vals)} values")
if not params:
    sys.exit(0)

db = sqlite3.connect("jobs.db")
tags = {i: (a, ti, dl) for i, a, ti, dl in db.execute(
    "select id, area, title, delisted_at from seen_jobs where id like ?", (f"wd_{tenant}_%",))}
total, full = ids_for({})
fin_all = {i for i in full if i in tags and tags[i][0] not in (None, "", "other")}
print(f"\nfull board fetched {len(full)}; stored {sum(i in tags for i in full)}; finance {len(fin_all)}")
for p, d, vals in facets:
    if p.split(">")[-1] not in params:
        continue
    print(f"\n=== {p} ({d})")
    rows = []
    for v in vals:
        n, ids = ids_for({p.split(">")[-1]: [v["id"]]})
        st = [i for i in ids if i in tags]
        fin = [i for i in st if tags[i][0] not in (None, "", "other")]
        rows.append((len(fin), len(st) - len(fin), n, v["descriptor"], v["id"], fin, [i for i in st if i not in fin]))
    rows.sort(key=lambda r: (-r[0], -r[2]))
    for f, o, n, desc, vid, fin, oth in rows:
        print(f"  fin={f:3d} other={o:3d} board={n:4d}  {desc}  [{vid}]")
        for i in fin:
            print(f"        + {tags[i][0]:16s} {tags[i][1][:80]}")
        if SHOW_OTHER:
            for i in oth[:6]:
                print(f"        - {tags[i][1][:80]}")
