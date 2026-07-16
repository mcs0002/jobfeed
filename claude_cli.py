"""Locate the `claude` CLI binary.

The tagger (tag.py) and cover_letter.py drive Haiku/Sonnet through the `claude`
CLI as a subprocess (shared auth — no separate ANTHROPIC_API_KEY needed), so
they share this one discovery helper instead of each hard-coding paths.
"""
import os
import shutil

# Order matters — `claude_bin` returns the first hit. Homebrew on macOS is the
# dev path; `~/.claude/local/claude` is the user-install path that
# launchd-spawned processes fall back to when PATH is stripped.
CLAUDE_BIN_CANDIDATES = (
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    os.path.expanduser("~/.claude/local/claude"),
    os.path.expanduser("~/.local/bin/claude"),
    "claude",
)

# Tool lockdown for the prompt-only invocations (tag.py, cover_letter.py):
# both callers inline every input in the prompt, so the CLI needs no tools at
# all. The prompts CONTAIN scraped job descriptions — text written by whoever
# controls a job board, i.e. hostile input. Without this, an injected
# instruction could drive Read/Bash from the process cwd (the project root,
# where .env and secrets/ live). Callers should also pass a neutral cwd
# (e.g. tempfile.gettempdir()) to subprocess.run as defense in depth.
NO_TOOLS_ARGS = (
    "--disallowedTools",
    "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit,TodoWrite",
)


def claude_bin() -> str | None:
    for cand in CLAUDE_BIN_CANDIDATES:
        if cand.startswith("/") and os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
        if not cand.startswith("/"):
            found = shutil.which(cand)
            if found:
                return found
    return None


# Back-compat alias for the old private name some modules imported.
_claude_bin = claude_bin
