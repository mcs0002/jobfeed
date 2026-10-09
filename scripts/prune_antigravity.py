#!/usr/bin/env python3
"""Delete Antigravity run artifacts older than a retention window.

Every attended application leaves about 8 MB behind: the parsed trajectory
(~1 MB), every accessibility snapshot it took, the saved output of each tool
call, and a screen recording of the browser session. After 117 runs that was
651 MB in `brain/` and 253 MB in `conversations/`, growing by roughly a
gigabyte a week, with no retention policy at all.

The window is the user's decision of 2026-09-17: two weeks is long enough to
audit a run (`skills/audit-run`), and nothing reads these afterwards.

Destructive, so dry-run by default — the same shape as
`scripts/reapply_guards.py`. `--apply` is the only thing that deletes.

    ssh m1 'cd ~/projects/job_scraper && .venv/bin/python \\
        scripts/prune_antigravity.py'            # report only
    ssh m1 'cd ~/projects/job_scraper && .venv/bin/python \\
        scripts/prune_antigravity.py --apply'
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("ANTIGRAVITY_HOME",
                           Path.home() / ".gemini" / "antigravity")).resolve()
BRAIN = ROOT / "brain"
CONVERSATIONS = ROOT / "conversations"
DEFAULT_DAYS = 14


def newest_mtime(path: Path) -> float:
    """The most recent mtime anywhere under `path`.

    A run directory's own mtime is not enough: it does not move when a file
    inside is rewritten, so a directory can look older than the work in it.
    Judging the tree by its newest file can only ever keep a run too long,
    which is the safe direction for a delete."""
    newest = 0.0
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    for current, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                newest = max(newest, os.lstat(os.path.join(current, name)).st_mtime)
            except OSError:
                continue
    return newest


def tree_size(path: Path) -> int:
    total = 0
    for current, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                continue
    return total


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit != "GB" else f"{size:.1f} GB"
        size /= 1024
    return f"{size:.1f} GB"


def _guard(path: Path) -> None:
    """Refuse to delete anything that is not inside the Antigravity root. The
    paths come from a glob of that root, so this can only fire on a symlink out
    of it or a mangled ANTIGRAVITY_HOME — both of which should stop the run."""
    resolved = path.resolve()
    if ROOT not in resolved.parents:
        raise SystemExit(f"refusing to delete outside {ROOT}: {resolved}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"retention window (default {DEFAULT_DAYS})")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete; without it nothing is removed")
    parser.add_argument("--conversations", action="store_true",
                        help="also delete the conversation databases. Off by "
                             "default: brain/ holds derived artifacts, but the "
                             "databases are Antigravity's own store and it may "
                             "still list those conversations in its UI.")
    args = parser.parse_args()

    if not BRAIN.is_dir():
        print(f"no Antigravity data at {ROOT}", file=sys.stderr)
        return 1

    cutoff = time.time() - args.days * 86400
    victims: list[tuple[Path, int]] = []
    kept = kept_bytes = 0

    for run in sorted(BRAIN.iterdir()):
        if not run.is_dir():
            continue
        if newest_mtime(run) >= cutoff:
            kept += 1
            kept_bytes += tree_size(run)
            continue
        victims.append((run, tree_size(run)))

    if args.conversations:
        for db in sorted(CONVERSATIONS.glob("*.db")):
            try:
                if db.stat().st_mtime < cutoff:
                    victims.append((db, db.stat().st_size))
            except OSError:
                continue

    freed = sum(size for _p, size in victims)
    verb = "deleting" if args.apply else "would delete"
    print(f"retention {args.days} days | keeping {kept} run(s), {human(kept_bytes)}")
    print(f"{verb} {len(victims)} item(s), {human(freed)}")
    for path, size in victims[:20]:
        age = (time.time() - newest_mtime(path)) / 86400
        print(f"  {human(size):>8}  {age:5.1f}d  {path.name}")
    if len(victims) > 20:
        print(f"  ... and {len(victims) - 20} more")

    if not args.apply:
        print("\nnothing was deleted. Re-run with --apply to remove them.")
        return 0

    removed = 0
    for path, _size in victims:
        _guard(path)
        try:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            removed += 1
        except OSError as exc:
            print(f"  could not remove {path.name}: {exc}", file=sys.stderr)
    print(f"removed {removed} of {len(victims)} item(s), {human(freed)} freed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
