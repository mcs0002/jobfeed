"""tal.net listing parser: per-tenant id scoping, and the cross-tenant
collision guard that protects the tenants wired before scoping existed."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobfeed import main  # noqa: E402
from jobfeed.db import JobDB  # noqa: E402
from scrapers import talnet  # noqa: E402
from scripts import rescope_ids  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TABLE = """
<div class="results_meta"><h2>2 results</h2></div>
<table class="solr_search_list">
  <tr class="details_row"><td><a class="subject"
    href="https://fidelityinternational.tal.net/vx/candidate/opp/123">Analyst</a></td></tr>
  <tr class="details_row"><td><a class="subject"
    href="https://fidelityinternational.tal.net/vx/candidate/opp/456">Associate</a></td></tr>
</table>
"""

TILES = """
<div class="results_meta"><h2>1 result</h2></div>
<ul><li class="opp-container">
  <a class="subject" href="https://lek.tal.net/vx/candidate/opp/789">Consultant</a>
  <div class="candidate-opp-field-3">
    <span class="candidate-opp-field-label">Location:</span> London</div>
</li></ul>
"""

# Tenants whose rows were stored under bare `<ats>_<n>` ids before per-tenant
# scoping (iCIMS: f8c84ed; tal.net: 2026-09-23). They stay bare so stored
# status, stars and applications keep their keys. Every other tenant scopes.
# One tenant left this list on 2026-10-07: its talnet_1427 hit another
# tenant's, so it was scoped "fil" and re-keyed by
# scripts/rescope_ids.py.
UNSCOPED_TENANTS = {
    "talnet": {
        "lek.tal.net", "evercore.tal.net",
        "bankcampuscareers.tal.net", "nomuracampus.tal.net",
        "morganstanley.tal.net", "rothschildandco.tal.net",
    },
    "icims": {
        "careers-adlittle.icims.com", "careers-stonex.icims.com",
        "careers-stifel.icims.com",
    },
}


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _session_for(page):
    class _Session:
        def get(self, url, **kw):
            return _Resp(page)
    return _Session


class TalnetIdTests(unittest.TestCase):
    def _scrape(self, page, **kw):
        with patch.object(talnet, "make_session", _session_for(page)), \
             patch.object(talnet, "fix_encoding", lambda r: None):
            return talnet.scrape("https://x.tal.net/board", fetch_detail=False,
                                 **kw)

    def test_table_ids_bare_without_scope_and_namespaced_with_it(self):
        self.assertEqual([j["id"] for j in self._scrape(TABLE)],
                         ["talnet_123", "talnet_456"])
        self.assertEqual([j["id"] for j in self._scrape(TABLE, id_scope="fil")],
                         ["talnet_fil_123", "talnet_fil_456"])

    def test_tile_ids_bare_without_scope_and_namespaced_with_it(self):
        self.assertEqual([j["id"] for j in self._scrape(TILES)], ["talnet_789"])
        self.assertEqual([j["id"] for j in self._scrape(TILES, id_scope="lek")],
                         ["talnet_lek_789"])

    def test_adapter_passes_the_target_scope(self):
        from scrapers import HANDLERS
        with patch.object(talnet, "scrape", return_value=[]) as scrape:
            HANDLERS["talnet"]({"board_url": "https://x.tal.net/b",
                                "talnet_id_scope": "x"})
            HANDLERS["talnet"]({"board_url": "https://x.tal.net/b"})
        self.assertEqual(scrape.call_args_list[0].kwargs["id_scope"], "x")
        self.assertEqual(scrape.call_args_list[1].kwargs["id_scope"], "")


def _tenant_and_scope(target):
    if target["ats"] == "talnet":
        return (urlparse(target["board_url"]).hostname,
                target.get("talnet_id_scope", ""))
    cfg = target["icims"]
    return urlparse(cfg["base_url"]).hostname, cfg.get("id_scope", "")


class TargetScopeRuleTests(unittest.TestCase):
    """A new per-tenant-id source must be scoped; a latent collision stays
    latent only if nobody adds a ninth bare tenant."""

    def setUp(self):
        with open(os.path.join(ROOT, "targets.json")) as f:
            self.targets = [t for t in json.load(f)
                            if t.get("ats") in main.PER_TENANT_ID_ATS]

    def test_only_grandfathered_tenants_emit_bare_ids(self):
        for t in self.targets:
            host, scope = _tenant_and_scope(t)
            if not scope:
                self.assertIn(host, UNSCOPED_TENANTS[t["ats"]],
                              f"{t['name']}: new {t['ats']} tenant needs an id "
                              "scope (talnet_id_scope / icims.id_scope)")

    def test_targets_on_one_tenant_share_one_scope(self):
        # Two boards on one tenant (Evercore, Evercore Campus) list the same
        # vacancy under the same number; differing scopes would store it twice.
        scopes: dict = {}
        for t in self.targets:
            host, scope = _tenant_and_scope(t)
            scopes.setdefault((t["ats"], host), set()).add(scope)
        for key, found in scopes.items():
            self.assertEqual(len(found), 1, f"{key}: mixed scopes {found}")

    def test_scopes_are_unique_across_tenants(self):
        seen: dict = {}
        for t in self.targets:
            host, scope = _tenant_and_scope(t)
            if scope:
                self.assertEqual(seen.setdefault((t["ats"], scope), host), host,
                                 f"scope {scope!r} reused across tenants")


class ForeignTenantTests(unittest.TestCase):
    def test_host_mismatch_is_a_collision(self):
        self.assertTrue(main.foreign_tenant(
            "https://lek.tal.net/vx/opp/1", "https://evercore.tal.net/vx/opp/1"))

    def test_same_host_is_not(self):
        # Evercore's two boards share a tenant, so a shared id is one role.
        self.assertFalse(main.foreign_tenant(
            "https://evercore.tal.net/a/opp/1", "https://evercore.tal.net/b/opp/1"))

    def test_unknown_host_is_not_evidence(self):
        self.assertFalse(main.foreign_tenant("/vx/opp/1",
                                             "https://evercore.tal.net/opp/1"))
        self.assertFalse(main.foreign_tenant("https://lek.tal.net/opp/1", None))


class CollisionGuardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "jobs.db")
        # Listed but not scraped this run, so its stored row is neither
        # purged as an orphan nor delisted.
        self.evercore = {"name": "Evercore", "ats": "talnet",
                         "board_url": "https://evercore.tal.net/b"}
        db = JobDB(self.db_path)
        db.mark_seen("talnet_123", company="Evercore", title="Analyst",
                     url="https://evercore.tal.net/vx/candidate/opp/123")
        db.conn.execute("UPDATE seen_jobs SET last_seen = '2026-01-01' "
                        "WHERE id = 'talnet_123'")
        db.conn.commit()
        db.conn.close()

    def _run(self, company, url):
        company = {**company, "ats": "talnet", "category": "Banks",
                   "verified": True}
        job = {"id": "talnet_123", "title": "Analyst", "url": url,
               "location": "London"}
        with patch.object(main, "DB_FILE", self.db_path), \
             patch.object(main, "load_targets",
                          return_value=[self.evercore, company]), \
             patch.object(main, "scrape_targets",
                          return_value=[(company, [job], None)]), \
             patch.object(main, "scrape_heavy_targets", return_value=[]), \
             patch.object(main, "_enrich_new_jobs"), \
             patch.object(main, "_write_health_state", return_value=set()), \
             patch.object(main.notify, "send_alert") as alert, \
             patch.object(sys, "argv", ["main.py", "--no-tag"]):
            main.main()
        db = JobDB(self.db_path)
        row = db.get_job("talnet_123")
        db.conn.close()
        return alert, row

    def test_other_tenant_same_id_alerts_and_leaves_the_row_alone(self):
        alert, row = self._run({"name": "L.E.K. Consulting"},
                               "https://lek.tal.net/vx/candidate/opp/123")
        subjects = [c.args[0] for c in alert.call_args_list]
        self.assertIn("job-scan: cross-tenant job id collision", subjects)
        self.assertEqual(row["company"], "Evercore")
        self.assertEqual(row["last_seen"], "2026-01-01")

    def test_same_tenant_sibling_board_is_not_a_collision(self):
        alert, row = self._run({"name": "Evercore (Campus)"},
                               "https://evercore.tal.net/vx/candidate/opp/123")
        subjects = [c.args[0] for c in alert.call_args_list]
        self.assertNotIn("job-scan: cross-tenant job id collision", subjects)
        self.assertNotEqual(row["last_seen"], "2026-01-01")


class RescopeMigrationTests(unittest.TestCase):
    """scripts/rescope_ids.py moves one tenant's rows, and everything keyed by
    them, to scoped ids and leaves the colliding tenant alone."""

    FIL = "https://fidelityinternational.tal.net/vx/candidate/opp/{}/en-GB"
    NOM = "https://nomuracampus.tal.net/vx/candidate/opp/{}/en-GB"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        db = JobDB(os.path.join(self.tmp, "jobs.db"))
        self.addCleanup(db.conn.close)
        self.con = db.conn
        db.mark_seen("talnet_1406", company="Fidelity International",
                     title="Equity Research", url=self.FIL.format(1406))
        db.mark_seen("talnet_1355", company="Fidelity International",
                     title="Analyst", url=self.FIL.format(1355))
        db.mark_seen("talnet_1427", company="Nomura (Campus)",
                     title="IBD Intern", url=self.NOM.format(1427))
        c = self.con
        c.execute("UPDATE seen_jobs SET status = 'rejected', favorite = 1 "
                  "WHERE id = 'talnet_1406'")
        for jid in ("talnet_1406", "talnet_1427"):
            c.execute("INSERT INTO job_descriptions (job_id, description) "
                      "VALUES (?, 'text')", (jid,))
        c.execute("INSERT INTO application_workflows (workflow_id, job_id, "
                  "application_url, created_at, updated_at) VALUES "
                  "('apply_1', 'talnet_1406', 'u', 't', 't')")
        c.execute("INSERT INTO application_mail_events (message_key, job_id) "
                  "VALUES ('m1', 'talnet_1406')")
        c.commit()

    def _ids(self, table, col):
        return sorted(r[0] for r in self.con.execute(f"SELECT {col} FROM {table}"))

    def test_rekeys_tenant_rows_and_their_dependants_only(self):
        mapping = rescope_ids.plan(self.con, "talnet",
                                   "fidelityinternational.tal.net", "fil")
        self.assertEqual(mapping, {"talnet_1406": "talnet_fil_1406",
                                   "talnet_1355": "talnet_fil_1355"})
        rescope_ids.rescope(self.con, mapping)
        self.assertEqual(self._ids("seen_jobs", "id"),
                         ["talnet_1427", "talnet_fil_1355", "talnet_fil_1406"])
        self.assertEqual(self._ids("job_descriptions", "job_id"),
                         ["talnet_1427", "talnet_fil_1406"])
        self.assertEqual(self._ids("application_workflows", "job_id"),
                         ["talnet_fil_1406"])
        self.assertEqual(self._ids("application_mail_events", "job_id"),
                         ["talnet_fil_1406"])
        self.assertEqual(self.con.execute(
            "SELECT status, favorite FROM seen_jobs WHERE id = 'talnet_fil_1406'"
        ).fetchone(), ("rejected", 1))
        # Idempotent: nothing bare left under the tenant.
        self.assertEqual(rescope_ids.plan(
            self.con, "talnet", "fidelityinternational.tal.net", "fil"), {})

    def test_existing_scoped_id_aborts_without_changes(self):
        self.con.execute("INSERT INTO seen_jobs (id, url) VALUES "
                         "('talnet_fil_1355', ?)", (self.FIL.format(1355),))
        self.con.commit()
        mapping = {"talnet_1355": "talnet_fil_1355"}
        with self.assertRaises(RuntimeError):
            rescope_ids.rescope(self.con, mapping)
        self.assertIn("talnet_1355", self._ids("seen_jobs", "id"))

    def test_unrelated_orphan_does_not_block_but_a_new_one_would(self):
        self.con.execute("INSERT INTO application_workflows (workflow_id, "
                         "job_id, application_url, created_at, updated_at) "
                         "VALUES ('apply_old', 'sf_gone', 'u', 't', 't')")
        self.con.commit()
        rescope_ids.rescope(self.con, {"talnet_1355": "talnet_fil_1355"})
        self.assertIn("talnet_fil_1355", self._ids("seen_jobs", "id"))

    def test_every_job_id_column_in_the_schema_is_listed(self):
        self.assertEqual(rescope_ids.unknown_job_id_columns(self.con), [])

    def test_fidelity_target_carries_the_migrated_scope(self):
        with open(os.path.join(ROOT, "targets.json")) as f:
            fil = [t for t in json.load(f)
                   if t.get("name") == "Fidelity International"]
        self.assertEqual([t.get("talnet_id_scope") for t in fil], ["fil"])


if __name__ == "__main__":
    unittest.main()
