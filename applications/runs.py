"""Per-run numbers for the Applications page, read from Antigravity's records.

Every question the audit answered by hand -- how many model generations a run
took, how many tool calls, how long each one cost, whether it raised a
permission prompt, re-read its snapshots, searched outside its granted files or
had to be restarted -- is answerable from the same trajectory files
`scripts/agaudit.py` reads. the user asked on 2026-09-18 to see them on the
site so the process can be watched without running an audit each time.

The parsing is `agaudit`'s own, imported rather than copied, so the page and the
audit cannot disagree about what a run did. Parsing every transcript on each
page load would cost seconds (161 conversations, some of several megabytes), so
each conversation's facts are cached against its transcript's and database's
mtime and size, and only a changed conversation is read again.
"""
from __future__ import annotations

import importlib.util
import json
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = ROOT / "antigravity_runs_cache.json"
CACHE_VERSION = 4


def _load_agaudit():
    spec = importlib.util.spec_from_file_location("agaudit", ROOT / "scripts" / "agaudit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agaudit = _load_agaudit()


def _stamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _signature(conv: str) -> list:
    sig = []
    for path in (agaudit.transcript_path(conv), agaudit.CONVERSATIONS / f"{conv}.db"):
        try:
            st = path.stat()
            sig.append([int(st.st_mtime), st.st_size])
        except OSError:
            sig.append(None)
    return sig


def run_facts(conv: str) -> dict[str, Any]:
    """Everything the page shows about one conversation."""
    rows = agaudit.read_transcript(conv)
    facts = agaudit.db_facts(conv)
    calls = list(agaudit.tool_calls(rows))
    names = [name for _, name, _ in calls]
    stamps = [s for s in (_stamp(r.get("created_at", "")) for r in rows) if s]
    media = agaudit.BRAIN / conv / ".tempmediaStorage"
    written = len(list(media.glob("snapshot_full_*.txt"))) if media.is_dir() else 0
    snap_reads = [str(a.get("AbsolutePath", "")) for _, n, a in calls
                  if n == "view_file" and "snapshot" in str(a.get("AbsolutePath", ""))]
    outside = 0
    for _, name, args in calls:
        if name not in agaudit.SEARCH_TOOLS:
            continue
        target = " ".join(str(args.get(k, "")) for k in
                          ("SearchPath", "AbsolutePath", "DirectoryPath", "Path"))
        if not any(g in target for g in agaudit.GRANTED_SEARCH_PATHS):
            outside += 1
    tabs = agaudit.tab_ownership(rows)
    events = agaudit.subagent_events(rows)
    return {
        "kills": events["kills"],
        "lists": events["lists"],
        "conv": conv,
        "own_tab": bool(tabs["owned"]),
        "tab_violations": len(tabs["violations"]),
        "workflow": agaudit.workflow_of(conv, rows),
        "run_id": agaudit.run_of(conv, rows),
        "kind": "sub" if any(n.startswith("mcp_chrome_devtools") for n in names) else "par",
        "started": stamps[0].isoformat() if stamps else "",
        "ended": stamps[-1].isoformat() if stamps else "",
        "seconds": int((stamps[-1] - stamps[0]).total_seconds()) if len(stamps) > 1 else 0,
        "steps": len(rows),
        "generations": facts["generations"],
        "model": facts["model"],
        "tool_calls": len(calls),
        "browser_calls": sum(1 for n in names if n.startswith("mcp_chrome_devtools")),
        "snapshots_written": written,
        "snapshot_reads": len(snap_reads),
        "snapshot_rereads": len(snap_reads) - len(set(snap_reads)),
        "failed_steps": len(facts["failed_steps"]),
        "prompts": len(facts["prompts"]),
        "outside_searches": outside,
    }


def all_runs(cache_file: Path = CACHE_FILE) -> list[dict[str, Any]]:
    """Facts for every conversation on this machine, newest first. Empty where
    Antigravity has never run, which is every machine but the M1."""
    if not agaudit.CONVERSATIONS.is_dir():
        return []
    try:
        cache = json.loads(cache_file.read_text())
        if cache.get("version") != CACHE_VERSION:
            cache = {}
    except (OSError, ValueError):
        cache = {}
    entries = cache.get("runs", {})
    fresh: dict[str, Any] = {}
    changed = False
    for db_path in agaudit.CONVERSATIONS.glob("*.db"):
        conv = db_path.stem
        sig = _signature(conv)
        old = entries.get(conv)
        if old and old.get("sig") == sig:
            fresh[conv] = old
            continue
        try:
            fresh[conv] = {"sig": sig, "facts": run_facts(conv)}
        except Exception:  # one unreadable run must not take the page down
            continue
        changed = True
    if changed or len(fresh) != len(entries):
        tmp = cache_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "runs": fresh}))
            os.replace(tmp, cache_file)
        except OSError:
            pass
    runs = [e["facts"] for e in fresh.values()]
    return sorted(runs, key=lambda r: r.get("started", ""), reverse=True)


def by_workflow(runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        if run.get("workflow"):
            out.setdefault(run["workflow"], []).append(run)
    for group in out.values():
        group.sort(key=lambda r: r.get("started", ""))
    return out




def workflow_stats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """One application's totals across every conversation that worked on it."""
    subs = [r for r in runs if r["kind"] == "sub"]
    sub_seconds = sum(r["seconds"] for r in subs)
    sub_generations = sum(r["generations"] for r in subs)
    return {
        "sessions": len(subs),
        "parents": len(runs) - len(subs),
        "agent_seconds": sub_seconds,
        "agent_time": _short(sub_seconds),
        "generations": sum(r["generations"] for r in runs),
        "sub_generations": sub_generations,
        "sec_per_generation": (round(sub_seconds / sub_generations, 1)
                               if sub_generations else None),
        "tool_calls": sum(r["tool_calls"] for r in subs),
        "browser_calls": sum(r["browser_calls"] for r in subs),
        "snapshots_written": sum(r["snapshots_written"] for r in subs),
        "snapshot_reads": sum(r["snapshot_reads"] for r in subs),
        "snapshot_rereads": sum(r["snapshot_rereads"] for r in subs),
        "failed_steps": sum(r["failed_steps"] for r in runs),
        "prompts": sum(r["prompts"] for r in runs),
        "outside_searches": sum(r["outside_searches"] for r in runs),
        "own_tab": bool(subs) and all(r.get("own_tab") for r in subs),
        "tab_violations": sum(r.get("tab_violations", 0) for r in subs),
        "model": next((r["model"] for r in subs if r["model"]), ""),
        "started": runs[0]["started"] if runs else "",
    }


def _short(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def overview(stats: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """Page-level numbers over the applications an agent actually worked on,
    for the last seven days and for all time."""
    now = now or datetime.now(timezone.utc)

    def window(cutoff: datetime | None) -> dict[str, Any]:
        rows = [s for s in stats if s["sessions"]
                and (cutoff is None or (_stamp(s["started"]) or now) >= cutoff)]
        spg = _median([s["sec_per_generation"] for s in rows if s["sec_per_generation"]])
        gens = _median([s["generations"] for s in rows])
        calls = _median([s["tool_calls"] for s in rows])
        agent = _median([s["agent_seconds"] for s in rows])
        restarted = sum(1 for s in rows if s["sessions"] > 1)
        return {
            "applications": len(rows),
            "median_agent_time": _short(agent),
            "median_generations": f"{gens:.0f}" if gens is not None else "—",
            "median_tool_calls": f"{calls:.0f}" if calls is not None else "—",
            "sec_per_generation": f"{spg:.1f}s" if spg is not None else "—",
            "restarted": (f"{restarted} ({round(100 * restarted / len(rows))}%)"
                          if rows else "—"),
            "prompts": sum(s["prompts"] for s in rows),
            "outside_searches": sum(s["outside_searches"] for s in rows),
            "tab_violations": sum(s.get("tab_violations", 0) for s in rows),
            "own_tab": (f"{sum(1 for s in rows if s.get('own_tab'))}/{len(rows)}"
                        if rows else "—"),
            "snapshot_rereads": sum(s["snapshot_rereads"] for s in rows),
            "failed_steps": sum(s["failed_steps"] for s in rows),
        }

    return {"recent": window(now - timedelta(days=7)), "all": window(None)}
