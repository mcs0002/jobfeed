"""Keep the application browser attachable and its uploads reachable.

Antigravity's browser tool (the chrome-devtools-mcp it bundles, run with
`--autoConnect`) finds Chrome only through
`~/Library/Application Support/Google/Chrome/DevToolsActivePort` and connects to
whatever port that file names. Since Chrome 155 (2026-10-12) the user's main
Chrome raises an "Allow remote debugging?" dialog for every new connection,
which no run waits for. So applications run in a dedicated Chrome instead: its
own profile (`PROFILE_DIR`) and a fixed port (`PORT`), started with the
command-line flag, which Chrome honours for a non-default profile without any
dialog. `ensure()` keeps it running and keeps that file pointing at it; the main
Chrome rewrites or deletes the file when it starts with its debugging toggle on,
so this runs from the M1 keepalive every five minutes and before each launch.
`applications/password.py` reads the same file and so types into the same
browser.

The same tool uploads only from its workspace roots, which from an Antigravity
conversation leaves the per-user temp directory. `stage_documents()` mirrors
every document the applicant profile names into `STAGE_ROOT`, and the profile's
`documents.base_dir` points there; `documents.source_dir` is the iCloud folder
they come from. The copies are real files, not links: the tool resolves links
and would refuse the target. macOS sweeps the temp directory, so the staging is
idempotent and rerun on the same schedule.

A run that still fails to attach is diagnosed in `skills/audit-run/SKILL.md`,
"A run that never reached the form".
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "secrets" / "applicant_profile.json"
PORT = 9333
PROFILE_DIR = Path.home() / "Library" / "Application Support" / "Jobfeed" / "chrome-apply"
PORT_FILE = (Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
             / "DevToolsActivePort")
CHROME_APP = "Google Chrome"
STAGE_NAME = "jobfeed-uploads"


def user_temp_dir() -> Path:
    """The per-user temp directory, the one root the browser tool uploads from.

    Read from `getconf` rather than `$TMPDIR`, which a launchd job or an SSH
    shell may not carry; the browser tool itself uses Node's `os.tmpdir()`,
    which is this directory under Antigravity."""
    try:
        out = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
        if out:
            return Path(out).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    import tempfile
    return Path(tempfile.gettempdir()).resolve()


def stage_root() -> Path:
    return user_temp_dir() / STAGE_NAME


def _version(timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version",
                                    timeout=timeout) as resp:
            return json.load(resp)
    except (OSError, ValueError):
        return None


def chrome_running() -> bool:
    out = subprocess.run(["pgrep", "-f", f"--user-data-dir={PROFILE_DIR}"],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())


def ensure_chrome(wait: float = 30.0) -> dict:
    """Start the dedicated Chrome if it is not answering on PORT."""
    info = _version()
    if info is not None:
        return {"chrome": "running", "browser": info.get("Browser", "")}
    if chrome_running():
        # Alive but not answering yet (still starting): give it the wait below.
        started = False
    else:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", "-gna", CHROME_APP, "--args",
                        f"--user-data-dir={PROFILE_DIR}",
                        f"--remote-debugging-port={PORT}",
                        "--no-first-run", "--no-default-browser-check"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = True
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        info = _version()
        if info is not None:
            return {"chrome": "started" if started else "running",
                    "browser": info.get("Browser", "")}
        time.sleep(1)
    raise RuntimeError(f"dedicated Chrome is not answering on port {PORT}")


def ensure_port_file(info: dict | None = None) -> str:
    """Point the main profile's DevToolsActivePort at the dedicated Chrome."""
    info = info or _version()
    if info is None:
        raise RuntimeError(f"dedicated Chrome is not answering on port {PORT}")
    ws = str(info["webSocketDebuggerUrl"])
    path = ws.split(f":{PORT}", 1)[1]
    want = f"{PORT}\n{path}"
    try:
        if PORT_FILE.read_text(encoding="ascii") == want:
            return "unchanged"
    except OSError:
        pass
    PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PORT_FILE.with_name(PORT_FILE.name + ".jobfeed-tmp")
    tmp.write_text(want, encoding="ascii")
    os.replace(tmp, PORT_FILE)
    return "rewritten"


def _copy_if_changed(src: Path, dst: Path) -> bool:
    if dst.is_file():
        s, d = src.stat(), dst.stat()
        if s.st_size == d.st_size and int(s.st_mtime) == int(d.st_mtime):
            return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)
    return True


def _documents(profile: dict) -> tuple[Path, dict[str, str]]:
    docs = profile.get("documents") or {}
    source = docs.get("source_dir") or docs.get("base_dir")
    if not source:
        raise RuntimeError("applicant profile has no documents.source_dir")
    files = {k: v for k, v in docs.items()
             if isinstance(v, str) and k not in {"base_dir", "source_dir"}
             and not k.endswith("_note") and Path(v).suffix}
    return Path(os.path.expanduser(source)), files


def stage_documents(profile_path: Path = PROFILE) -> dict:
    """Mirror every document the profile names into the upload staging root."""
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    source, files = _documents(profile)
    root = stage_root() / "documents"
    copied, missing = [], []
    for key, rel in files.items():
        src = source / rel
        if not src.is_file():
            missing.append(key)
            continue
        if _copy_if_changed(src, root / rel):
            copied.append(key)
    return {"stage": str(root), "copied": copied, "missing": missing,
            "count": len(files) - len(missing)}


def stage_file(src: Path, rel: str) -> Path:
    """Stage one generated file (a writer PDF) and return its uploadable path."""
    dst = stage_root() / rel
    _copy_if_changed(src, dst)
    return dst


def ensure() -> dict:
    result: dict = {}
    info = ensure_chrome()
    result.update(info)
    result["port_file"] = ensure_port_file()
    result["documents"] = stage_documents()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["ensure", "stage"], nargs="?", default="ensure")
    args = parser.parse_args()
    try:
        out = ensure() if args.command == "ensure" else stage_documents()
    except Exception as exc:  # report, never a traceback in the keepalive log
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(dict(out, status="ok")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
