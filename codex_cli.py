"""Locate and safely configure the Codex CLI for prompt-only tagging.

``tag.py`` passes the complete classification payload over stdin. The job
descriptions are untrusted text from public career sites, so the Codex process
must not expose shell, browser, app, or other agent tools to that payload.
"""
import os
import shutil


# Prefer the CLI bundled with the ChatGPT desktop app on machines where it is
# present, then fall back to the normal standalone installation locations.
CODEX_BIN_CANDIDATES = (
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    os.path.expanduser("~/.local/bin/codex"),
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    "codex",
)


# Codex exec is an agent harness by default. Tagging is deliberately reduced
# to a prompt-in/final-message-out operation: no user config, rules, skills, or
# tools are loaded, and the subprocess runs ephemerally in a neutral directory.
NO_TOOLS_ARGS = (
    "--ignore-user-config",
    "--ignore-rules",
    "--disable", "shell_tool",
    "--disable", "apps",
    "--disable", "browser_use",
    "--disable", "computer_use",
    "--disable", "image_generation",
    "--disable", "multi_agent",
    "--disable", "skill_search",
)


def codex_bin() -> str | None:
    for candidate in CODEX_BIN_CANDIDATES:
        if candidate.startswith("/"):
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


_codex_bin = codex_bin
