"""Compare a source's current scope against a candidate scope.

usage: python scripts/scope_eval.py candidates.json [name ...]
candidates.json maps target name -> dict of keys to override on the target
(a key set to null is removed). Prints, per source: current vs candidate row
counts, finance-tagged roles kept and LOST (titles), other-tagged roles
dropped, and unstored candidate rows (never seen: new or hard-gated).
"""
import copy
import json
import sqlite3
import sys

sys.path.insert(0, ".")
from jobfeed.main import scrape_company

cands = json.load(open(sys.argv[1]))
names = sys.argv[2:] or list(cands)
t = json.load(open("targets.json"))
t = t if isinstance(t, list) else t.get("targets", t)
db = sqlite3.connect("jobs.db")
FIN = lambda a: a not in (None, "", "other")

for name in names:
    cur = next(x for x in t if x.get("name") == name)
    new = copy.deepcopy(cur)
    for k, v in cands[name].items():
        if v is None:
            new.pop(k, None)
        else:
            new[k] = v
    a, ea = scrape_company(cur)
    b, eb = scrape_company(new)
    ia = {j["id"]: j["title"] for j in a}
    ib = {j["id"]: j["title"] for j in b}
    ids = list(set(ia) | set(ib))
    tags = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f"select id, area from seen_jobs where id in ({','.join('?' * len(chunk))})"
        tags.update(dict(db.execute(q, chunk)))
    fin_a = {i for i in ia if FIN(tags.get(i))}
    oth_a = {i for i in ia if tags.get(i) == "other"}
    lost = fin_a - set(ib)
    print(f"\n## {name}: current {len(ia)} (err={ea}) -> candidate {len(ib)} (err={eb})")
    print(f"   finance kept {len(fin_a) - len(lost)}/{len(fin_a)}; "
          f"other dropped {len(oth_a - set(ib))}/{len(oth_a)}; "
          f"candidate other kept {len([i for i in ib if tags.get(i) == 'other'])}; "
          f"candidate unstored {len([i for i in ib if i not in tags])}")
    for i in sorted(lost, key=lambda i: ia[i]):
        print(f"   LOST {tags[i]:16s} {ia[i][:90]}")
    gained = [i for i in ib if i not in ia]
    if gained:
        print(f"   gained (not in current scrape) {len(gained)}: " + "; ".join(ib[i][:40] for i in gained[:8]))
