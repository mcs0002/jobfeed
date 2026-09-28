import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from applications import autopilot  # noqa: E402
from jobfeed.db import JobDB  # noqa: E402

T0 = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def conv(cid, workflow, kind, started, ended, *, run_id="", kills=(), lists=(),
         tab_violations=0):
    return {"conv": cid, "workflow": workflow, "kind": kind, "started": iso(started),
            "ended": iso(ended), "run_id": run_id, "kills": list(kills),
            "lists": list(lists), "tab_violations": tab_violations}


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = JobDB(self.tmp.name)
        self.default_settings = self.db.autopilot_settings()
        self.db.set_autopilot(enabled=True)
        self.db.conn.execute(
            "UPDATE application_autopilot SET foreign_confirmed_at=? WHERE id=1",
            (iso(T0 - timedelta(days=1)),))
        self.db.conn.commit()
        self.launched = []
        self.wf = {}
        for jid in ("j1", "j2", "j3"):
            self.db.conn.execute(
                "INSERT INTO seen_jobs (id, company, title, url, first_seen, last_seen, status) "
                "VALUES (?, 'Co', 'Analyst', 'https://ats.example/job', '2026-09-19', '2026-09-19', 'new')",
                (jid,))
            self.db.conn.commit()
            workflow, _ = self.db.create_application_workflow(jid, f"https://ats.example/{jid}")
            self.wf[jid] = workflow["workflow_id"]

    def tearDown(self):
        self.db.conn.close()
        os.unlink(self.tmp.name)

    def tick(self, runs=(), now=T0):
        return autopilot.tick(self.db, runs=list(runs), now=now, launch=self.launched.append)

    def finish_launch(self, workflow_id, ok=True):
        wf = self.db.get_application_workflow(workflow_id)
        self.db.claim_application_launch(wf["launch_request_id"])
        self.db.finish_application_launch(wf["launch_request_id"], ok, "" if ok else "helper_error")

    def report(self, workflow_id, status):
        self.db.transition_application_workflow(workflow_id, status, actor="agent")

    def test_database_refuses_a_second_active_run(self):
        a, _ = self.db.queue_application_run(self.wf["j1"])
        b, _ = self.db.queue_application_run(self.wf["j2"])
        self.assertIsNotNone(self.db.acquire_application_run(a["run_id"]))
        self.assertIsNone(self.db.acquire_application_run(b["run_id"]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("UPDATE application_runs SET state='running' WHERE run_id=?",
                                 (b["run_id"],))

    def test_fresh_database_defaults_autopilot_disabled(self):
        self.assertFalse(self.default_settings["enabled"])

    def test_queueing_is_idempotent_per_workflow(self):
        a, created = self.db.queue_application_run(self.wf["j1"])
        again, created_again = self.db.queue_application_run(self.wf["j1"])
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(a["run_id"], again["run_id"])

    def test_paused_queue_starts_nothing(self):
        self.db.set_autopilot(enabled=False)
        self.db.queue_application_run(self.wf["j1"], manual=True)
        self.assertEqual([], self.tick())
        self.assertEqual([], self.launched)
        self.db.set_autopilot(enabled=True, paused_reason="zombie")
        self.tick()
        self.assertEqual([], self.launched)
        self.db.set_autopilot(paused_reason="")
        self.tick()
        self.assertEqual(1, len(self.launched))

    def test_manual_start_jumps_the_queue(self):
        self.db.queue_application_run(self.wf["j1"])
        self.db.queue_application_run(self.wf["j2"], manual=True, priority=10)
        self.tick()
        self.assertEqual([self.wf["j2"]], [w["workflow_id"] for w in self.launched])

    def test_full_cycle_waits_for_the_kill_then_starts_the_next(self):
        self.db.set_autopilot(enabled=True)
        first, _ = self.db.queue_application_run(self.wf["j1"])
        self.db.queue_application_run(self.wf["j2"])
        self.tick(now=T0)
        self.assertEqual("launching", self.db.get_application_run(first["run_id"])["state"])
        self.finish_launch(self.wf["j1"])
        launched_at = datetime.fromisoformat(self.db.get_application_run(first["run_id"])["launching_at"])
        self.tick(now=launched_at + timedelta(seconds=30))
        self.assertEqual("running", self.db.get_application_run(first["run_id"])["state"])

        par = conv("p1-aaaaaaaa", self.wf["j1"], "par", launched_at,
                   launched_at + timedelta(minutes=12), run_id=first["run_id"])
        sub = conv("s1-aaaaaaaa", self.wf["j1"], "sub", launched_at + timedelta(seconds=20),
                   launched_at + timedelta(minutes=11), run_id=first["run_id"])
        self.report(self.wf["j1"], "review_ready")
        self.tick([par, sub], now=launched_at + timedelta(minutes=12))
        self.assertEqual("draining", self.db.get_application_run(first["run_id"])["state"])
        # An outcome is not a stop: nothing else may launch yet.
        self.assertEqual(1, len(self.launched))

        par["kills"] = [{"at": iso(launched_at + timedelta(minutes=12, seconds=5)),
                         "ids": ["s1-aaaaaaaa"], "ok": True}]
        self.tick([par, sub], now=launched_at + timedelta(minutes=13))
        done = self.db.get_application_run(first["run_id"])
        self.assertEqual("review_ready", done["state"])
        self.assertEqual("manage_subagents_kill", done["stop_source"])
        self.tick([par, sub], now=launched_at + timedelta(minutes=13, seconds=20))
        self.assertEqual([self.wf["j1"], self.wf["j2"]], [w["workflow_id"] for w in self.launched])

    def test_needs_user_action_parks_and_releases(self):
        run, _ = self.db.queue_application_run(self.wf["j1"], manual=True)
        self.tick()
        self.finish_launch(self.wf["j1"])
        start = datetime.fromisoformat(self.db.get_application_run(run["run_id"])["launching_at"])
        self.tick(now=start + timedelta(seconds=10))
        self.report(self.wf["j1"], "needs_user_action")
        par = conv("p", self.wf["j1"], "par", start, start + timedelta(minutes=4),
                   run_id=run["run_id"],
                   lists=[{"at": iso(start + timedelta(minutes=4)), "active": 0, "ids": []}])
        sub = conv("s", self.wf["j1"], "sub", start, start + timedelta(minutes=3),
                   run_id=run["run_id"])
        self.tick([par, sub], now=start + timedelta(minutes=4))
        self.tick([par, sub], now=start + timedelta(minutes=5))
        self.assertEqual("parked", self.db.get_application_run(run["run_id"])["state"])
        self.assertIsNone(self.db.active_application_run())

    def test_no_stop_evidence_blocks_and_asks_for_confirmation(self):
        run, _ = self.db.queue_application_run(self.wf["j1"], manual=True)
        self.db.queue_application_run(self.wf["j2"], manual=True)
        self.tick()
        self.finish_launch(self.wf["j1"])
        start = datetime.fromisoformat(self.db.get_application_run(run["run_id"])["launching_at"])
        self.tick(now=start)
        self.report(self.wf["j1"], "failed")
        sub = conv("s", self.wf["j1"], "sub", start, start + timedelta(minutes=2),
                   run_id=run["run_id"])
        self.tick([sub], now=start + timedelta(minutes=3))
        self.tick([sub], now=start + timedelta(minutes=20))
        held = self.db.get_application_run(run["run_id"])
        self.assertEqual("draining", held["state"])
        self.assertEqual(1, held["recovery_required"])
        self.assertEqual(1, len(self.launched))
        autopilot.confirm_stopped(self.db, run["run_id"], now=start + timedelta(minutes=21))
        self.assertEqual("owner_confirmed", self.db.get_application_run(run["run_id"])["stop_source"])
        self.tick([sub], now=start + timedelta(minutes=22))
        self.assertEqual(2, len(self.launched))

    def test_a_kill_before_the_agents_last_step_is_not_evidence(self):
        start = T0
        sub = conv("s", "w", "sub", start, start + timedelta(minutes=10))
        par = conv("p", "w", "par", start, start + timedelta(minutes=10),
                   kills=[{"at": iso(start + timedelta(minutes=5)), "ids": ["s"], "ok": True}])
        stopped, _ = autopilot.stop_evidence([par], [sub])
        self.assertIsNone(stopped)

    def test_live_foreign_agent_blocks_the_start(self):
        self.db.queue_application_run(self.wf["j1"], manual=True)
        stray = conv("stray", "", "sub", T0 - timedelta(minutes=5),
                     T0 - timedelta(minutes=3))
        log = self.tick([stray], now=T0)
        self.assertEqual([], self.launched)
        self.assertTrue(any("not ours" in line for line in log))

    def test_zombie_after_confirmed_stop_pauses_autopilot(self):
        # IMC QR 2026-09-18: an agent resumed after its run was over.
        self.db.set_autopilot(enabled=True)
        run, _ = self.db.queue_application_run(self.wf["j1"])
        self.tick()
        self.finish_launch(self.wf["j1"])
        start = datetime.fromisoformat(self.db.get_application_run(run["run_id"])["launching_at"])
        self.tick(now=start)
        self.report(self.wf["j1"], "failed")
        kill_at = start + timedelta(minutes=3)
        par = conv("p", self.wf["j1"], "par", start, kill_at,
                   run_id=run["run_id"],
                   kills=[{"at": iso(kill_at), "ids": ["s"], "ok": True}])
        sub = conv("s", self.wf["j1"], "sub", start, start + timedelta(minutes=2),
                   run_id=run["run_id"])
        self.tick([par, sub], now=kill_at)
        self.tick([par, sub], now=kill_at + timedelta(seconds=10))
        self.assertEqual("failed", self.db.get_application_run(run["run_id"])["state"])
        sub["ended"] = iso(kill_at + timedelta(minutes=5))
        self.db.queue_application_run(self.wf["j2"])
        self.tick([par, sub], now=kill_at + timedelta(minutes=6))
        self.assertIn("after its stop", self.db.autopilot_settings()["paused_reason"])
        self.assertEqual(1, len(self.launched))

    def test_agent_spawned_after_confirmed_stop_pauses_autopilot(self):
        # Fidelity 2026-09-19: the parent started a replacement agent under the
        # parked attempt 23 seconds after the kill, and killed it 15 s later.
        self.db.set_autopilot(enabled=True)
        run, _ = self.db.queue_application_run(self.wf["j1"])
        self.tick()
        self.finish_launch(self.wf["j1"])
        start = datetime.fromisoformat(self.db.get_application_run(run["run_id"])["launching_at"])
        self.tick(now=start)
        self.report(self.wf["j1"], "needs_user_action")
        kill_at = start + timedelta(minutes=1)
        par = conv("p", self.wf["j1"], "par", start, kill_at,
                   run_id=run["run_id"],
                   kills=[{"at": iso(kill_at), "ids": ["s"], "ok": True}])
        sub = conv("s", self.wf["j1"], "sub", start, start + timedelta(seconds=40),
                   run_id=run["run_id"])
        self.tick([par, sub], now=kill_at)
        self.tick([par, sub], now=kill_at + timedelta(seconds=10))
        self.assertEqual("parked", self.db.get_application_run(run["run_id"])["state"])
        late = conv("s2", self.wf["j1"], "sub", kill_at + timedelta(seconds=23),
                    kill_at + timedelta(seconds=38), run_id=run["run_id"])
        par["kills"].append({"at": iso(kill_at + timedelta(seconds=38)), "ids": ["s2"], "ok": True})
        self.tick([par, sub, late], now=kill_at + timedelta(seconds=40))
        self.assertIn("after its stop", self.db.autopilot_settings()["paused_reason"])
        # He looks and resumes: the dead agent must not re-pause the queue.
        self.db.set_autopilot(enabled=True, paused_reason="")
        self.db.conn.execute("UPDATE application_autopilot SET foreign_confirmed_at=? WHERE id=1",
                             (iso(kill_at + timedelta(minutes=5)),))
        self.tick([par, sub, late], now=kill_at + timedelta(minutes=5, seconds=15))
        self.assertEqual("", self.db.autopilot_settings()["paused_reason"])
        # But the same agent acting again afterwards pauses it again.
        late["ended"] = iso(kill_at + timedelta(minutes=6))
        self.tick([par, sub, late], now=kill_at + timedelta(minutes=6, seconds=15))
        self.assertIn("after its stop", self.db.autopilot_settings()["paused_reason"])

    def test_launcher_failure_releases_and_pauses(self):
        self.db.set_autopilot(enabled=True)
        run, _ = self.db.queue_application_run(self.wf["j1"])
        self.tick()
        self.finish_launch(self.wf["j1"], ok=False)
        self.tick(now=T0 + timedelta(minutes=1))
        self.assertEqual("failed", self.db.get_application_run(run["run_id"])["state"])
        self.assertIsNone(self.db.active_application_run())
        self.assertIn("launcher failed", self.db.autopilot_settings()["paused_reason"])

    def test_a_second_attempt_relaunches_a_launched_workflow(self):
        run, _ = self.db.queue_application_run(self.wf["j1"], manual=True)
        self.tick()
        self.finish_launch(self.wf["j1"])
        autopilot.confirm_stopped(self.db, run["run_id"])
        again, created = self.db.queue_application_run(self.wf["j1"], manual=True)
        self.assertTrue(created)
        self.assertEqual(2, again["attempt_no"])
        self.tick(now=T0 + timedelta(hours=1))
        self.assertEqual(2, len(self.launched))

    def test_prior_attempt_records_cannot_attach_to_retry(self):
        old, _ = self.db.queue_application_run(self.wf["j1"], manual=True)
        self.tick()
        autopilot.confirm_stopped(self.db, old["run_id"])
        new, _ = self.db.queue_application_run(self.wf["j1"], manual=True)
        self.tick(now=T0 + timedelta(seconds=30))
        prior = [
            conv("old-parent", self.wf["j1"], "par", T0, T0 + timedelta(seconds=25),
                 run_id=old["run_id"],
                 lists=[{"at": iso(T0 + timedelta(seconds=25)), "active": 0, "ids": []}]),
            conv("old-sub", self.wf["j1"], "sub", T0, T0 + timedelta(seconds=20),
                 run_id=old["run_id"]),
        ]
        parents, subs = autopilot.run_conversations(prior, new["run_id"])
        self.assertEqual(([], []), (parents, subs))

    def test_retry_resets_the_prior_terminal_workflow_outcome(self):
        old, _ = self.db.queue_application_run(self.wf["j1"], manual=True)
        self.tick()
        self.report(self.wf["j1"], "failed")
        autopilot.confirm_stopped(self.db, old["run_id"])
        new, _ = self.db.queue_application_run(self.wf["j1"], manual=True)
        self.tick(now=T0 + timedelta(minutes=1))
        self.assertEqual("queued", self.db.get_application_workflow(self.wf["j1"])["status"])
        self.finish_launch(self.wf["j1"])
        self.tick(now=T0 + timedelta(minutes=2))
        self.assertEqual("running", self.db.get_application_run(new["run_id"])["state"])
        self.tick(now=T0 + timedelta(minutes=3))
        self.assertEqual("running", self.db.get_application_run(new["run_id"])["state"])

    def test_kill_thirty_seconds_before_last_step_is_not_evidence(self):
        sub = conv("s", "w", "sub", T0, T0 + timedelta(minutes=10))
        par = conv("p", "w", "par", T0, T0 + timedelta(minutes=10),
                   kills=[{"at": iso(T0 + timedelta(minutes=9, seconds=30)),
                           "ids": ["s"], "ok": True}])
        self.assertIsNone(autopilot.stop_evidence([par], [sub])[0])
        par = conv("p", "w", "par", T0, T0 + timedelta(minutes=10),
                   lists=[{"at": iso(T0 + timedelta(minutes=9, seconds=30)),
                           "active": 0, "ids": []}])
        self.assertIsNone(autopilot.stop_evidence([par], [sub])[0])

    def test_tab_violation_pauses_and_holds_the_lease(self):
        run, _ = self.db.queue_application_run(self.wf["j1"], manual=True)
        self.tick()
        start = datetime.fromisoformat(self.db.get_application_run(run["run_id"])["launching_at"])
        sub = conv("bad-tab", self.wf["j1"], "sub", start, start + timedelta(seconds=30),
                   run_id=run["run_id"], tab_violations=1)
        self.tick([sub], now=start + timedelta(minutes=1))
        held = self.db.get_application_run(run["run_id"])
        self.assertEqual("launching", held["state"])
        self.assertEqual(1, held["recovery_required"])
        self.assertIn("tab ownership", self.db.autopilot_settings()["paused_reason"])

    def test_existing_enabled_install_is_disabled_once_by_safety_migration(self):
        path = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE application_autopilot (id INTEGER PRIMARY KEY, "
                    "enabled INTEGER NOT NULL, paused_reason TEXT NOT NULL DEFAULT '', "
                    "paused_at TEXT, updated_at TEXT)")
        con.execute("INSERT INTO application_autopilot (id, enabled) VALUES (1, 1)")
        con.commit()
        con.close()
        migrated = JobDB(path)
        try:
            settings = migrated.autopilot_settings()
            self.assertFalse(settings["enabled"])
            self.assertIn("qualification", settings["paused_reason"])
            migrated.set_autopilot(enabled=True, paused_reason="")
        finally:
            migrated.conn.close()
        reopened = JobDB(path)
        try:
            self.assertTrue(reopened.autopilot_settings()["enabled"])
        finally:
            reopened.conn.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
