"""Serial autopilot for attended applications (notes/ATTENDED_AUTOMATION.md,
implementation steps 2 and 3).

Exactly one browser agent may be alive at a time, because every Antigravity
conversation drives Chrome through one shared chrome-devtools-mcp with one
"selected page". The queue therefore advances only when the previous agent is
positively confirmed stopped, and never on a workflow outcome alone: on
2026-09-18 an IMC agent that had reported `failed` resumed a minute later, just
as its replacement started, and for eight minutes two agents clicked on one tab.

The lifecycle of a run (`application_runs`):

    queued -> launching -> running -> draining -> review_ready | parked | failed

`launching`, `running` and `draining` hold the one worker lease, enforced by a
partial unique index in the database. `draining` starts when the workflow
reports its outcome and ends only on stop evidence from the Antigravity
controller: the parent's `manage_subagents` kill that succeeded for each browser
subagent of the run, or a `list` showing none active. Silence, a timeout or the
agent saying it stopped do not count. Without evidence the run waits in
`draining`, the queue stays blocked, and the page offers the user an explicit
"agent is stopped" confirmation, which is recorded as the stop source.

`tick()` holds no state of its own. It re-derives everything from the database
and from Antigravity's records, so a web-service or machine restart resumes
exactly where it was, and a tick can run as often as wanted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from jobfeed.db import JobDB

# A run whose agents have written nothing for this long is not presumed dead;
# it is flagged for recovery and the queue waits for the user.
STALE_AFTER = timedelta(minutes=30)
# Outcome reported but no stop evidence after this long: ask him to confirm.
DRAIN_GRACE = timedelta(minutes=10)
# The launcher helper claims a request within seconds; a launch still pending
# after this long means the helper is not running, and no agent was started.
LAUNCH_TIMEOUT = timedelta(minutes=5)
# Slack used only when comparing recorded activity with a confirmed stop in the
# zombie detector. Attempt ownership itself is exact by run_id.
CLOCK_SLACK = timedelta(seconds=60)

OUTCOME_STATE = {
    "review_ready": "review_ready",
    "completed": "review_ready",
    "needs_user_action": "parked",
    "failed": "failed",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def run_conversations(runs: list[dict], run_id: str) -> tuple[list, list]:
    """Parent and browser-subagent conversations for exactly one attempt."""
    mine = [r for r in runs if r.get("run_id") == run_id]
    return ([r for r in mine if r.get("kind") == "par"],
            [r for r in mine if r.get("kind") == "sub"])


def stop_evidence(parents: list[dict], subs: list[dict]) -> tuple[datetime | None, str]:
    """When the attempt's browser agents were positively stopped, and how.

    Every subagent must be covered: a successful kill naming it, or a parent
    list showing no active subagents, at or after that subagent's last step.
    With no subagent at all, a list showing none active is required, because a
    parent can still spawn one. Returns (None, reason) otherwise."""
    kills = [(k, _at(k["at"])) for p in parents for k in p.get("kills") or [] if k.get("ok")]
    # A list result only proves the state of the parent that asked. When an
    # attempt has multiple parent conversations, it is not safe global proof.
    empties = ([_at(entry["at"]) for entry in parents[0].get("lists") or []
                if entry.get("active") == 0] if len(parents) == 1 else [])
    if not subs:
        stamps = [t for t in empties if t]
        return (max(stamps), "list_empty") if stamps else (None, "no stop evidence yet")
    stopped: list[datetime] = []
    sources: list[str] = []
    for sub in subs:
        last = _at(sub.get("ended")) or _at(sub.get("started"))
        by_kill = [t for k, t in kills if t and sub["conv"] in k["ids"]
                   and (last is None or t >= last)]
        by_list = [t for t in empties if t and (last is None or t >= last)]
        if by_kill:
            found, source = min(by_kill), "manage_subagents_kill"
        elif by_list:
            found, source = min(by_list), "list_empty"
        else:
            return None, f"browser agent {sub['conv'][:8]} not confirmed stopped"
        stopped.append(found)
        sources.append(source)
    source = sources[0] if len(set(sources)) == 1 else "kill_and_list_empty"
    return max(stopped), source


def live_foreign_agents(runs: list[dict], confirmed_after: datetime | None,
                        known_run_ids: set[str]) -> list[dict]:
    """Unconfirmed browser agents that are not owned by a database attempt.

    They never age out from silence. Owner resume establishes a durable cutoff
    after the owner checked old/manual conversations; every later foreign agent
    blocks until positive stop evidence or another explicit owner resume.
    """
    parents_by_key: dict[tuple[str, str], list[dict]] = {}
    for r in runs:
        if r.get("kind") == "par":
            key = (r.get("run_id") or "", r.get("workflow") or "")
            parents_by_key.setdefault(key, []).append(r)
    live = []
    for sub in runs:
        if sub.get("kind") != "sub" or sub.get("run_id") in known_run_ids:
            continue
        started = _at(sub.get("started")) or _at(sub.get("ended"))
        if confirmed_after and started and started <= confirmed_after:
            continue
        key = (sub.get("run_id") or "", sub.get("workflow") or "")
        stopped, _ = stop_evidence(parents_by_key.get(key, []), [sub])
        if stopped is None:
            live.append(sub)
    return live


def _last_activity(parents: list[dict], subs: list[dict]) -> datetime | None:
    stamps = [_at(r.get("ended")) for r in parents + subs]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def tick(db: JobDB, *, runs: list[dict] | None = None, now: datetime | None = None,
         launch: Callable[[dict], Any] | None = None) -> list[str]:
    """Advance the queue by at most one transition per run. Returns a short log
    of what it did, for tests and the service log."""
    if runs is None:
        from applications import runs as antigravity_runs
        runs = antigravity_runs.all_runs()
    if launch is None:
        from applications.launcher import enqueue_launch as launch
    now = now or _now()
    log: list[str] = []

    active = db.active_application_run()
    if active is not None:
        log.extend(_advance(db, active, runs, now, launch))
        active = db.active_application_run()
    log.extend(_zombie_scan(db, runs, now))
    if active is not None:
        return log

    # Read after the zombie scan, which may just have paused the queue.
    settings = db.autopilot_settings()
    if settings["paused_reason"]:
        return log
    eligible = db.list_application_runs(states=("queued",))
    if not settings["enabled"]:
        # Default-off disables unattended discovery/advancement, not a launch
        # the user explicitly queued with the Start button.
        eligible = [r for r in eligible if r["manual"]]
    if not eligible:
        return log
    known_run_ids = {r["run_id"] for r in db.list_application_runs(limit=1000)}
    foreign = live_foreign_agents(
        runs, _at(settings.get("foreign_confirmed_at")), known_run_ids)
    if foreign:
        reason = f"browser agent {foreign[0]['conv'][:8]} is unconfirmed and not ours"
        db.set_autopilot(paused_reason=reason)
        log.append(f"waiting: {reason}")
        return log
    run = db.acquire_application_run(eligible[0]["run_id"])
    if run is None:
        return log
    log.extend(_start(db, run, launch))
    return log


def _start(db: JobDB, run: dict, launch: Callable[[dict], Any]) -> list[str]:
    try:
        workflow = db.get_application_workflow(run["workflow_id"])
        if workflow and workflow["status"] != "queued":
            db.transition_application_workflow(
                run["workflow_id"], "queued", detail="", actor="owner")
        workflow, created = db.request_application_launch(
            run["workflow_id"], retry=True, new_attempt=True)
        if created:
            launch(workflow)
    except Exception as exc:  # the lease is ours; give it back on a failed start
        now = _iso(_now())
        db.update_application_run(
            run["run_id"], expect_state="launching", state="failed", finished_at=now,
            agent_stopped_at=now, stop_source="never_started", outcome="failed",
            error_code="launch_request_failed")
        db.set_autopilot(paused_reason=f"launch request failed: {type(exc).__name__}")
        return [f"{run['run_id']}: launch request failed, autopilot paused"]
    return [f"{run['run_id']}: launching {run['workflow_id']}"]


def _advance(db: JobDB, run: dict, runs: list[dict], now: datetime,
             launch: Callable[[dict], Any]) -> list[str]:
    workflow = db.get_application_workflow(run["workflow_id"]) or {}
    since = _at(run["launching_at"])
    parents, subs = run_conversations(runs, run["run_id"])
    last = _last_activity(parents, subs)
    if last and (not run["last_activity_at"] or _iso(last) > run["last_activity_at"]):
        run = db.update_application_run(run["run_id"], last_activity_at=_iso(last)) or run
    rid = run["run_id"]

    breached = next((s for s in subs if int(s.get("tab_violations") or 0) > 0), None)
    if breached:
        reason = f"browser agent {breached['conv'][:8]} violated tab ownership"
        if not run["recovery_required"]:
            db.update_application_run(
                rid, recovery_required=1, recovery_reason=reason)
        db.set_autopilot(paused_reason=reason)
        return [f"{rid}: tab ownership breach, autopilot paused"]

    if run["state"] == "launching":
        status = workflow.get("launch_status")
        if status == "succeeded":
            db.update_application_run(rid, expect_state="launching", state="running",
                                      running_at=_iso(now))
            return [f"{rid}: running"]
        if status == "failed":
            db.update_application_run(
                rid, expect_state="launching", state="failed", finished_at=_iso(now),
                agent_stopped_at=_iso(now), stop_source="never_started", outcome="failed",
                error_code=workflow.get("launch_error_code") or "launch_failed")
            db.set_autopilot(paused_reason="launcher failed: "
                             + (workflow.get("launch_error_code") or "unknown"))
            return [f"{rid}: launch failed, autopilot paused"]
        if since and now - since > LAUNCH_TIMEOUT and status == "pending" and not subs:
            db.update_application_run(
                rid, expect_state="launching", state="failed", finished_at=_iso(now),
                agent_stopped_at=_iso(now), stop_source="never_started", outcome="failed",
                error_code="launcher_not_running")
            db.set_autopilot(paused_reason="the launcher did not pick up the request")
            return [f"{rid}: launcher timeout, autopilot paused"]
        return []

    if run["state"] == "running":
        outcome = workflow.get("status", "")
        reported = _at(workflow.get("updated_at"))
        if outcome in OUTCOME_STATE and reported and since and reported >= since:
            db.update_application_run(rid, expect_state="running", state="draining",
                                      draining_at=_iso(now), outcome=outcome)
            return [f"{rid}: draining after {outcome}"]
        ref = last or _at(run["running_at"]) or since
        if ref and now - ref > STALE_AFTER and not run["recovery_required"]:
            db.update_application_run(rid, recovery_required=1,
                                      recovery_reason="no agent activity for 30 minutes")
            return [f"{rid}: stale, recovery required"]
        return []

    if run["state"] == "draining":
        stopped, source = stop_evidence(parents, subs)
        if stopped is not None:
            final = OUTCOME_STATE.get(run["outcome"], "failed")
            db.update_application_run(
                rid, expect_state="draining", state=final, finished_at=_iso(now),
                agent_stopped_at=_iso(stopped), stop_source=source,
                recovery_required=0, recovery_reason="")
            return [f"{rid}: stopped ({source}), {final}"]
        drained = _at(run["draining_at"])
        if drained and now - drained > DRAIN_GRACE and not run["recovery_required"]:
            db.update_application_run(rid, recovery_required=1, recovery_reason=source)
            return [f"{rid}: no stop evidence, recovery required"]
    return []


def _zombie_scan(db: JobDB, runs: list[dict], now: datetime) -> list[str]:
    """A browser agent that acted after its run was confirmed stopped is the
    exact failure the lease exists to prevent: pause everything."""
    log = []
    # Resume stamps the moment he looked. An agent whose last step precedes it
    # has been seen; only one still acting afterwards pauses again. Without this
    # the resume's own tick re-found Fidelity's dead agent and re-paused at once.
    looked = _at(db.autopilot_settings().get("foreign_confirmed_at"))
    recent = [r for r in db.list_application_runs(states=("review_ready", "parked", "failed"),
                                                   limit=40)
              if r["agent_stopped_at"] and r["stop_source"] not in ("never_started",)
              and (_at(r["finished_at"]) or now) > now - timedelta(hours=12)]
    for run in recent:
        stopped = _at(run["agent_stopped_at"])
        _, subs = run_conversations(runs, run["run_id"])
        # Two shapes. An agent alive at the stop that kept acting, and a new
        # agent spawned under the finished attempt: Fidelity 2026-09-19, whose
        # parent read the launcher's echoed opening message as a re-send and
        # started a replacement 23 seconds after its stop was confirmed, while
        # Deutsche Bank already held the lease. A resume is always a new
        # attempt, so no agent may start under this run_id after its stop.
        late = [s for s in subs
                if (_at(s.get("started")) or stopped) > stopped
                or (_at(s.get("ended")) or stopped) > stopped + CLOCK_SLACK]
        late = [s for s in late
                if not looked or (_at(s.get("ended")) or _at(s.get("started")) or now) > looked]
        if late and not db.autopilot_settings()["paused_reason"]:
            db.set_autopilot(paused_reason=(
                f"browser agent {late[0]['conv'][:8]} acted after its stop was confirmed"))
            log.append(f"{run['run_id']}: zombie agent, autopilot paused")
    return log


def confirm_stopped(db: JobDB, run_id: str, now: datetime | None = None) -> dict | None:
    """the user's explicit statement that the run's agent is stopped, for when
    Antigravity has produced no evidence. Recorded as such, never inferred."""
    run = db.get_application_run(run_id)
    if run is None or run["state"] not in ("launching", "running", "draining"):
        return None
    now = now or _now()
    final = OUTCOME_STATE.get(run["outcome"], "failed") if run["state"] == "draining" else "failed"
    return db.update_application_run(
        run_id, expect_state=run["state"], state=final, finished_at=_iso(now),
        agent_stopped_at=_iso(now), stop_source="owner_confirmed",
        outcome=run["outcome"] or "failed", recovery_required=0, recovery_reason="")


def cancel_queued(db: JobDB, run_id: str) -> dict | None:
    return db.update_application_run(run_id, expect_state="queued", state="cancelled",
                                     finished_at=_iso(_now()))
