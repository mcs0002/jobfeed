"""Locate the `claude` CLI binary.

The tagger (jobfeed/tag.py) and the other prompt-only callers drive models through the `claude`
CLI as a subprocess (shared auth — no separate ANTHROPIC_API_KEY needed), so
they share this one discovery helper instead of each hard-coding paths.
"""
import os
import shutil
import subprocess
import tempfile
import threading

# Order matters — `claude_bin` returns the first hit. The native installer
# (~/.local/bin, self-updating) is preferred over the Homebrew cask, which sits
# frozen at whatever version was last `brew upgrade`d (the M1's cask is months
# stale while ~/.local/bin tracks current).
CLAUDE_BIN_CANDIDATES = (
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/.claude/local/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "claude",
)

# Tool lockdown for the prompt-only invocations (jobfeed/tag.py and the rest):
# every caller inline every input in the prompt, so the CLI needs no tools at
# all. The prompts CONTAIN scraped job descriptions — text written by whoever
# controls a job board, i.e. hostile input. Without this, an injected
# instruction could drive Read/Bash from the process cwd (the project root,
# where .env and secrets/ live). Callers should also pass a neutral cwd
# (e.g. tempfile.gettempdir()) to subprocess.run as defense in depth.
NO_TOOLS_ARGS = (
    "--disallowedTools",
    "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,TodoWrite",
)


_WARM_LOCK = threading.Lock()
# The OUTCOME of the one warm attempt, not merely that it happened. None = not
# attempted yet. Storing a bare "ran" flag made every call after a failed warm
# return True, i.e. report the CLI as warm on the strength of an attempt that
# had just proved it was not. No caller branched on the result, so it never bit;
# the first one to do so would have got a wrong answer from a function whose
# whole contract is that answer.
_WARM_RESULT: bool | None = None


def warm_auth(bin_path: str, model: str, timeout: int = 90) -> bool:
    """Refresh the OAuth access token once, serially, before any fan-out.

    The stored refresh token rotates on use. When several `claude` subprocesses
    start with an already-expired access token they all refresh at once, and the
    losers of that race write back a token the server has already retired — so
    ~/.claude/.credentials.json is left holding dead credentials and every later
    run fails with "OAuth session expired and could not be refreshed" until a
    human runs the CLI by hand. That is what killed the nightly tagger from
    2026-08-15 (the 4-worker backfill_tags pass raced, and the next three scans
    all had to buy their tags from the paid API fallback).

    One serialized call up front means the parallel callers find a valid token
    and never refresh. Runs at most once per process, success or failure — a
    dead CLI is the API fallback's problem, not something to retry here — and
    every later call is answered from the remembered outcome.
    """
    global _WARM_RESULT
    with _WARM_LOCK:
        if _WARM_RESULT is not None:
            return _WARM_RESULT
        try:
            proc = subprocess.run(
                [bin_path, "-p", *NO_TOOLS_ARGS, "--model", model, "ok"],
                capture_output=True, text=True, timeout=timeout,
                cwd=tempfile.gettempdir(),
            )
        except (subprocess.SubprocessError, OSError):
            _WARM_RESULT = False
        else:
            _WARM_RESULT = proc.returncode == 0
        return _WARM_RESULT


def claude_bin() -> str | None:
    for cand in CLAUDE_BIN_CANDIDATES:
        if cand.startswith("/") and os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
        if not cand.startswith("/"):
            found = shutil.which(cand)
            if found:
                return found
    return None
