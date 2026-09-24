#!/usr/bin/env python3
"""Read an attended-application run out of Antigravity's own records.

Runs are audited after the fact — what the agent filled, what it got wrong,
what it wasted. The obvious source is the conversation SQLite at
`~/.gemini/antigravity/conversations/<id>.db`, whose `steps.step_payload` is a
protobuf blob: every audit until 2026-09-17 scraped it with regexes, which is
slow to write and loses whatever the regex did not anticipate.

Antigravity also writes the same trajectory as parsed JSONL, one object per
step, at `brain/<id>/.system_generated/logs/transcript_full.jsonl`, carrying
`tool_calls`, `content` and — the part no audit had ever read — `thinking`, the
model's own reasoning for each step. A run's mistakes are usually visible there
as they are made: the Goldman run of 2026-09-17 explains in `thinking` why it
chose a Pass/Fail grading scale, and the UBS Beijing run why it typed
"Caucasian". This reads the JSONL and falls back to the database only for what
the JSONL does not hold (permission prompts, model name, generation count).

Lives on the M1, where Antigravity runs:
    ssh m1 'cd ~/projects/job_scraper && .venv/bin/python scripts/agaudit.py list'
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("ANTIGRAVITY_HOME",
                           Path.home() / ".gemini" / "antigravity"))
CONVERSATIONS = ROOT / "conversations"
BRAIN = ROOT / "brain"
JOBS_DB = Path(os.environ.get("JOBS_DB",
                              Path.home() / "projects/job_scraper/jobs.db"))
# steps.status is indexed and 3 means the step completed. Anything else is a
# failed step, which is how to find errors without reading a single payload.
STATUS_OK = 3
WORKFLOW_RE = re.compile(r"\b(apply_[0-9a-f]{32})\b")
RUN_RE = re.compile(r"\b(run_[0-9a-f]{32})\b")


def transcript_path(conv: str) -> Path:
    return BRAIN / conv / ".system_generated" / "logs" / "transcript_full.jsonl"


def read_transcript(conv: str) -> list[dict]:
    path = transcript_path(conv)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _db(conv: str) -> sqlite3.Connection | None:
    path = CONVERSATIONS / f"{conv}.db"
    if not path.is_file():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _printable(blob) -> str:
    if not blob:
        return ""
    text = bytes(blob).decode("utf-8", "replace")
    return re.sub(r"\s+", " ", "".join(c if c.isprintable() else " " for c in text))


def db_facts(conv: str) -> dict:
    """What only the database holds: the model, how many generations it took,
    which steps failed, and which raised a permission prompt."""
    out = {"model": "", "generations": 0, "failed_steps": [], "prompts": []}
    con = _db(conv)
    if con is None:
        return out
    try:
        out["generations"] = con.execute(
            "SELECT count(*) FROM gen_metadata").fetchone()[0]
        for (blob,) in con.execute("SELECT data FROM gen_metadata LIMIT 1"):
            found = re.search(r"gemini-[a-z0-9.\-]+", _printable(blob))
            out["model"] = found.group(0) if found else ""
        out["failed_steps"] = [
            idx for (idx,) in con.execute(
                "SELECT idx FROM steps WHERE status <> ?", (STATUS_OK,))]
        out["prompts"] = [
            (idx, _printable(perm)[:120]) for idx, perm in con.execute(
                "SELECT idx, permissions FROM steps "
                "WHERE permissions IS NOT NULL AND length(permissions) > 0")]
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def _id_of(conv: str, rows: list[dict], pattern: re.Pattern) -> str:
    for row in rows[:6]:
        found = pattern.search(json.dumps(row))
        if found:
            return found.group(1)
    con = _db(conv)
    if con is None:
        return ""
    try:
        for (blob,) in con.execute(
                "SELECT step_payload FROM steps ORDER BY idx LIMIT 4"):
            found = pattern.search(_printable(blob))
            if found:
                return found.group(1)
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return ""


def workflow_of(conv: str, rows: list[dict]) -> str:
    return _id_of(conv, rows, WORKFLOW_RE)


def run_of(conv: str, rows: list[dict]) -> str:
    return _id_of(conv, rows, RUN_RE)


def role_of(workflow: str) -> str:
    if not workflow or not JOBS_DB.is_file():
        return ""
    try:
        con = sqlite3.connect(f"file:{JOBS_DB}?mode=ro", uri=True)
        row = con.execute(
            "SELECT j.company, j.title, w.status FROM application_workflows w "
            "JOIN seen_jobs j ON j.id = w.job_id WHERE w.workflow_id = ?",
            (workflow,)).fetchone()
        con.close()
    except sqlite3.Error:
        return ""
    return f"{row[0]} | {row[1][:52]} [{row[2]}]" if row else ""


def tool_calls(rows: list[dict]):
    for row in rows:
        for call in row.get("tool_calls") or []:
            yield row, call.get("name", "?"), call.get("args") or {}


def _stamp(row: dict) -> str:
    return str(row.get("created_at") or "")[11:19]


def summarise(conv: str) -> None:
    rows = read_transcript(conv)
    facts = db_facts(conv)
    workflow = workflow_of(conv, rows)
    calls = list(tool_calls(rows))
    names: dict[str, int] = {}
    for _, name, _args in calls:
        names[name] = names.get(name, 0) + 1

    stamps = [_stamp(r) for r in rows if _stamp(r)]
    span = f"{stamps[0]} -> {stamps[-1]}" if stamps else "unknown"
    media = BRAIN / conv / ".tempmediaStorage"
    taken = len(list(media.glob("snapshot_full_*.txt"))) if media.is_dir() else 0
    read_paths = [str(a.get("AbsolutePath", "")) for _, n, a in calls
                  if n == "view_file"]
    snaps = [p for p in read_paths if "snapshot" in p]
    repeats = len(snaps) - len(set(snaps))

    print(f"conversation  {conv}")
    print(f"workflow      {workflow or '(none found)'}")
    if workflow:
        print(f"role          {role_of(workflow) or '(not in jobs.db)'}")
    print(f"span          {span}")
    print(f"steps         {len(rows)}")
    print(f"model         {facts['model'] or '(unknown)'} "
          f"({facts['generations']} generations)")
    print(f"tool calls    {len(calls)}")
    print(f"snapshots     {taken} written, {len(snaps)} read"
          + (f", {repeats} RE-READ (loop)" if repeats else ""))
    print(f"failed steps  {facts['failed_steps'] or 'none'}")
    if facts["prompts"]:
        print(f"PERMISSION PROMPTS ({len(facts['prompts'])}):")
        for idx, text in facts["prompts"]:
            print(f"  [{idx}] {text}")
    else:
        print("prompts       none")
    print_search_findings(calls, facts["failed_steps"])
    print_tab_findings(rows)
    print("tools         " + ", ".join(
        f"{n} x{c}" for n, c in sorted(names.items(), key=lambda kv: -kv[1])))


# The rule the agent is held to is "a search outside the granted paths raises a
# permission prompt that blocks the run". So the test is the path, not the tool.
# Twice on 2026-09-17 this check called a compliant run an offender by reading the
# ban as absolute: Aurora for ten greps of its own snapshots, then Cargill for two
# greps of `applicant_profile.json`, which is one of the files the prompt
# explicitly grants. Neither could raise a prompt and neither did. An audit that
# cries wolf on obedient runs is worse than no audit, because the real breaks stop
# being legible.
GRANTED_SEARCH_PATHS = (
    ".gemini/antigravity/",                                   # its own workspace
    "secrets/applicant_profile.json",
    "skills/assisted-apply/SKILL.md",
    "skills/assisted-apply/references/form-filling.md",
    "skills/assisted-apply/references/documents-writing.md",
    "skills/assisted-apply/references/standard_questions.md",
)
# Grepping a granted FILE is fine once it has been read whole, which is the cheap
# way to re-confirm one value late in a long run. Grepping it INSTEAD of reading it
# is the failure the rule exists for: a Citi run on 2026-09-17 read SKILL.md as
# three line ranges and then searched for the rules it had skipped.
SEARCH_TOOLS = ("grep_search", "find_by_name", "list_dir")


def outside_reads(calls: list) -> list[tuple[int, str]]:
    """view_file calls on a path outside the granted files, as (step, path).

    SKILL.md allows view_file on the four initial files (and its own snapshots),
    nothing else. The World Bank run of 2026-09-23 opened his transcript PDF to
    "verify it exists" and sat on a permission prompt for 70 s; the database
    recorded no prompt, only a failed step, so neither check here saw it."""
    out = []
    for row, name, args in calls:
        if name != "view_file":
            continue
        path = str(args.get("AbsolutePath", ""))
        if not any(g in path for g in GRANTED_SEARCH_PATHS):
            out.append((int(row.get("step_index") or 0), path))
    return out


def print_search_findings(calls: list, failed_steps: list | None = None) -> None:
    read_whole: set[str] = set()
    granted = 0
    unread: list[str] = []
    outside: dict[str, int] = {}
    for _, name, args in calls:
        target = " ".join(str(args.get(k, "")) for k in
                          ("SearchPath", "AbsolutePath", "DirectoryPath", "Path"))
        if name == "view_file":
            read_whole.add(target.strip())
            continue
        if name not in SEARCH_TOOLS:
            continue
        hit = next((g for g in GRANTED_SEARCH_PATHS if g in target), None)
        if hit is None:
            outside[name] = outside.get(name, 0) + 1
        elif hit.endswith((".json", ".md")) and target.strip() not in read_whole:
            unread.append(target.strip().rsplit("/", 1)[-1])
        else:
            granted += 1
    if outside:
        print("RULE BREAK    " + ", ".join(f"{n} x{c}" for n, c in outside.items())
              + "  (searched outside the granted paths)")
    if unread:
        print("RULE BREAK    searched a granted file it never read whole: "
              + ", ".join(sorted(set(unread))))
    if granted:
        print(f"granted searches {granted} (own snapshots / granted files, after reading them)")
    failed = set(failed_steps or [])
    for step, path in outside_reads(calls):
        blocked = any(step + d in failed for d in (0, 1, 2))
        print(f"RULE BREAK    view_file outside the granted files at step {step}: "
              f"{path.rsplit('/', 1)[-1]}"
              + ("  (next step failed: likely a blocked permission prompt)" if blocked else ""))


# Tab ownership (notes/ATTENDED_AUTOMATION.md, implementation step 1). Every
# conversation shares one chrome-devtools-mcp process with one selected page, and
# until 2026-09-19 each run adopted whatever tab was selected: Gunvor navigated
# Glencore's tab, Glencore navigated IMC's. A run must open its own tab with
# new_page first, and may then act only on tabs it opened itself. The owned set
# comes from the "[selected]" line in each new_page result, and every later tool
# result names the selected page, so an action on a foreign tab is visible here.
_SELECTED_RE = re.compile(r"(\d+): \S+ \[selected\]")
_PAGE_MUTATIONS = frozenset({
    "navigate_page", "click", "fill", "fill_form", "type_text", "press_key",
    "upload_file", "evaluate_script", "drag", "hover", "handle_dialog",
})


def tab_ownership(rows: list[dict]) -> dict:
    """Which tabs the run opened itself, and every action outside them."""
    owned: list[int] = []
    violations: list[str] = []
    pending = ""
    adopted = False  # one finding per adoption, not one per later action
    for row in rows:
        calls = row.get("tool_calls") or []
        if calls:
            for call in calls:
                name = str(call.get("name", "")).replace("mcp_chrome_devtools_", "")
                args = call.get("args") or {}
                step = row.get("step_index")
                if name in _PAGE_MUTATIONS and not owned:
                    if not adopted:
                        violations.append(
                            f"step {step}: {name} on the preselected tab before opening its own")
                        adopted = True
                elif name in ("select_page", "close_page") and owned:
                    target = args.get("pageId")
                    if target is not None and int(target) not in owned:
                        violations.append(f"step {step}: {name} on tab {target}, not its own")
                elif name in ("select_page", "close_page") and not owned and not adopted:
                    violations.append(
                        f"step {step}: {name} on the preselected tab before opening its own")
                    adopted = True
                pending = name
            continue
        if row.get("type") != "GENERIC":
            continue
        found = _SELECTED_RE.search(str(row.get("content") or ""))
        if not found:
            continue
        selected = int(found.group(1))
        if pending == "new_page":
            if selected not in owned:
                owned.append(selected)
        elif owned and selected not in owned and pending in _PAGE_MUTATIONS:
            violations.append(f"step {row.get('step_index')}: {pending} landed on tab {selected}")
        pending = ""
    return {"owned": owned, "violations": violations}


# Controller-level evidence that a browser subagent has stopped. The parent's
# manage_subagents kill answers "Successfully killed N subagent(s) and their
# descendants", and list answers "You have N active subagent(s): [...]". Idle
# subagents still count as active until killed (52 of 58 recorded lists showed
# one), and "Subagent ... has gone idle" is sent mid-run while a subagent waits
# for a letter, so neither silence nor idleness is a stop. A kill that succeeded,
# or a list showing none, is. The serial autopilot releases its worker lease on
# this and nothing weaker.
_CONV_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_KILLED_RE = re.compile(r"Successfully killed (\d+) subagent")
_ACTIVE_RE = re.compile(r"You have (\d+) active subagent\(s\)")


def subagent_events(rows: list[dict]) -> dict:
    """Kills and lists a parent ran, with their results and times."""
    kills: list[dict] = []
    lists: list[dict] = []
    pending: tuple | None = None
    for row in rows:
        calls = row.get("tool_calls") or []
        if calls:
            pending = None
            for call in calls:
                if call.get("name") != "manage_subagents":
                    continue
                args = call.get("args") or {}
                action = str(args.get("Action", "")).lower()
                if action in ("kill", "list"):
                    pending = (action, [str(c) for c in args.get("ConversationIds") or []])
            continue
        content = str(row.get("content") or "")
        if pending is None or row.get("type") != "GENERIC":
            continue
        action, ids = pending
        at = str(row.get("created_at") or "")
        if action == "kill":
            killed = _KILLED_RE.search(content)
            kills.append({"at": at, "ids": ids, "ok": bool(killed and int(killed.group(1)) >= 1)})
        else:
            active = _ACTIVE_RE.search(content)
            if active:
                lists.append({"at": at, "active": int(active.group(1)),
                              "ids": _CONV_ID_RE.findall(content)})
        pending = None
    return {"kills": kills, "lists": lists}


def print_tab_findings(rows: list[dict]) -> None:
    if not any(str(n).startswith("mcp_chrome_devtools") for _, n, _ in tool_calls(rows)):
        return
    found = tab_ownership(rows)
    if found["violations"]:
        print(f"RULE BREAK    tab ownership x{len(found['violations'])}: "
              + "; ".join(found["violations"][:4]))
    else:
        print(f"own tab       {', '.join(map(str, found['owned'])) or 'none'}")


def show_report(conv: str) -> None:
    """The last message the subagent sent its parent: the review report."""
    rows = read_transcript(conv)
    messages = [args.get("Message", "") for _, name, args in tool_calls(rows)
                if name == "send_message"]
    if not messages:
        print("no send_message found (is this the parent conversation?)")
        return
    print(messages[-1])


def show_thinking(conv: str, pattern: str = "") -> None:
    """The model's own reasoning, optionally only where it mentions `pattern`.

    This is the channel that explains a wrong answer at the moment it was
    chosen, rather than leaving it to be inferred from the actions."""
    needle = pattern.lower()
    for row in read_transcript(conv):
        text = " ".join(str(row.get("thinking") or "").split())
        if not text or (needle and needle not in text.lower()):
            continue
        print(f"--- step {row.get('step_index')} {_stamp(row)}")
        print(text[:1800])


def search(pattern: str, limit: int = 40) -> None:
    """Every run whose transcript mentions `pattern`, newest first. Finds the
    run that saw a question before you know which conversation it was."""
    needle = pattern.lower()
    found = []
    for path in BRAIN.glob("*/.system_generated/logs/transcript_full.jsonl"):
        try:
            if needle in path.read_text(errors="replace").lower():
                found.append((path.stat().st_mtime, path.parents[2].name))
        except OSError:
            continue
    for mtime, conv in sorted(found, reverse=True)[:limit]:
        when = datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
        workflow = workflow_of(conv, read_transcript(conv)[:6])
        print(f"{when}  {conv}  {role_of(workflow) or workflow}")


def list_runs(limit: int) -> None:
    paths = sorted(CONVERSATIONS.glob("*.db"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:limit]
    for path in paths:
        conv = path.stem
        rows = read_transcript(conv)
        workflow = workflow_of(conv, rows)
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")
        # The browser subagent is the one holding the CDP tools; the parent
        # holds the terminal. Both send messages, so that is no discriminator.
        kind = "sub " if any(n.startswith("mcp_chrome_devtools")
                             for _, n, _ in tool_calls(rows)) else "par "
        print(f"{when}  {kind}{len(rows):>4} steps  {conv}  "
              f"{role_of(workflow) or workflow or ''}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("list", help="recent runs, newest first")
    one.add_argument("--limit", type=int, default=12)
    for name, helptext in (("summary", "metrics for one run"),
                           ("report", "the run's final review report"),
                           ("thinking", "the model's reasoning")):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("conversation")
        if name == "thinking":
            cmd.add_argument("pattern", nargs="?", default="")
    finder = sub.add_parser("search", help="runs whose transcript mentions text")
    finder.add_argument("pattern")

    args = parser.parse_args()
    if args.cmd == "list":
        list_runs(args.limit)
    elif args.cmd == "summary":
        summarise(args.conversation)
    elif args.cmd == "report":
        show_report(args.conversation)
    elif args.cmd == "thinking":
        show_thinking(args.conversation, args.pattern)
    elif args.cmd == "search":
        search(args.pattern)
    return 0


if __name__ == "__main__":
    sys.exit(main())
