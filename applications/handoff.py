"""Single source of truth for the two-message Antigravity handoff."""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlencode, urlparse

# The helper is invoked by absolute path so the command text is identical no
# matter what directory Antigravity happens to be in.
ROOT_HINT = str(Path(__file__).resolve().parent.parent)


# The three attended-workflow CLIs each read their request from a fixed path in
# /private/tmp, because Antigravity records a permission grant as the exact
# command text and a varying argument would mean a fresh prompt every time (see
# applications/status.py). That directory is world-writable. The sticky bit stops
# another local account deleting or renaming a file it does not own, but it does
# NOT stop one creating the name first — so a symlink planted at the path before
# the agent writes would redirect the read at an arbitrary file, and a regular
# file planted there would be parsed as the request.
#
# Every check below runs against the OPEN DESCRIPTOR rather than the path, so
# there is no window between deciding the file is safe and reading it.
def read_fixed_request(path: Path, *, max_bytes: int = 1 << 20) -> dict:
    """Parse one agent-written fixed request file, or raise ValueError.

    Refuses a symlink, anything that is not a regular file, a file owned by
    another user, and a file any other account can rewrite. Reads at most
    `max_bytes` so a file that has been swollen cannot be pulled into memory
    whole. The caller still validates the request's own shape; this only
    establishes that the bytes came from a file we wrote ourselves.
    """
    try:
        # O_NONBLOCK matters as much as O_NOFOLLOW here: a FIFO planted at the
        # path would otherwise park this process in open() until someone opened
        # the write end, which is the cheapest possible denial of the whole
        # attended workflow. On a regular file it does nothing.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        # O_NOFOLLOW refusing a symlink surfaces as ELOOP; name it rather than
        # reporting a generic read failure. errno.ELOOP, not the literal — it is
        # 62 on macOS and 40 on Linux.
        raise ValueError(
            "request path is a symlink" if exc.errno == errno.ELOOP
            else f"request unreadable: {exc}"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("request is not a regular file")
        if info.st_uid != os.getuid():
            raise ValueError("request is owned by another user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("request is writable by another account")
        if info.st_size > max_bytes:
            raise ValueError("request exceeds the size limit")
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise ValueError("request exceeds the size limit")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"request is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("request must be a JSON object")
    return obj


def safe_application_url(value: str) -> str:
    value = (value or "").strip()
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and bool(parsed.netloc) else ""


def workflow_token(secret: str, workflow_id: str) -> str:
    payload = f"application-workflow:{workflow_id}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def prior_applications_note(prior: list[dict]) -> str:
    if not prior:
        return (" Applications from Jobfeed right now: none at this firm in the last "
                "12 months, so a question whether this is his first application to "
                "the firm is Yes.")
    listed = "; ".join(
        f"{a['title']} ({a['status']}, applied {(a.get('applied_at') or '')[:10] or 'date unknown'})"
        for a in prior[:5])
    return (f" Applications from Jobfeed right now: he has already applied to this firm "
            f"in the last 12 months: {listed}. So this is NOT his first application: "
            "answer any previous-application question truthfully, and never tick a "
            "box confirming a first or only application; stop with needs_user_action "
            "and quote it.")


def handoff_view(workflow: dict, secret: str, public_base_url: str,
                 offer_elsewhere: bool | None = None,
                 prior_applications: list[dict] | None = None) -> dict:
    item = dict(workflow)
    token = workflow_token(secret, item["workflow_id"])
    callback = (
        f"{public_base_url.rstrip('/')}/application/{item['workflow_id']}/agent?"
        + urlencode({"token": token})
    )
    item["browser_command"] = f"/browser {item['application_url']}"
    item["agent_status_url"] = callback
    item["agent_status_post_url"] = (
        f"{public_base_url.rstrip('/')}/application/{item['workflow_id']}/agent/status"
    )
    # One unvarying command, so a single Antigravity permission grant covers
    # every status update. The curl this replaced carried the token, the state
    # and a free-text detail in the string, and each variation was a fresh
    # prompt: nine of them piled up after three applications.
    item["agent_status_request_file"] = "/private/tmp/the user-application-status-request.json"
    item["agent_status_command"] = (
        f"{ROOT_HINT}/.venv/bin/python -B {ROOT_HINT}/bin/application-status "
        "--fixed-request-file"
    )
    run_id = str(item.get("run_id") or "")
    attempt = f" Attempt {run_id}." if run_id else ""
    run_field = f', "run_id": "{run_id}"' if run_id else ""
    item["followup_prompt"] = (
        f"Use /assisted-apply for workflow {item['workflow_id']}.{attempt} "
        f"To report status, write {item['agent_status_request_file']} as "
        f'{{"workflow_id": "{item["workflow_id"]}"{run_field}, '
        f'"status": STATE, "detail": "SHORT NOTE"}} '
        "with STATE one of in_progress, needs_user_action, review_ready, failed, "
        f"then run: {item['agent_status_command']}. "
        "Never submit the application or mark it completed."
    )
    # Live from the board at launch, so the answer to "offers or late-stage
    # processes elsewhere?" is true on the day the form is filled.
    if offer_elsewhere is not None:
        item["followup_prompt"] += (
            " Recruiting status from Jobfeed right now: he holds an offer at another "
            "firm, so answer Yes to questions about offers or late-stage processes "
            "elsewhere and ask him for any details the form wants."
            if offer_elsewhere else
            " Recruiting status from Jobfeed right now: no offer at any other firm, "
            "so answer No to questions about offers or late-stage processes elsewhere."
        )
    # Same reason, same source: a form asking whether this is his first
    # application to the firm has only the board to go on.
    if prior_applications is not None:
        item["followup_prompt"] += prior_applications_note(prior_applications)
    # Antigravity queues a message typed while a turn is running and delivers it
    # only when that turn ends. On 2026-09-12 the whole Trafigura application ran
    # inside the first turn, so the context arrived at step 97 of 105 and framed
    # nothing. The first message therefore has to be self-sufficient.
    item["primary_prompt"] = f"{item['browser_command']}\n\n{item['followup_prompt']}"
    # The second message stays: it re-states "never submit" once the work is done,
    # and it keeps the helper's messages[1] prefix check satisfied without a
    # recompile, which would cost another Accessibility re-grant. It must not
    # read as a fresh request, though: it lands whenever the first turn ends,
    # and on 2026-09-19 a Fidelity parent that had just parked on a CAPTCHA took
    # the verbatim copy for the user re-sending the task, killed its agent and
    # spawned a replacement after the queue had moved on to Deutsche Bank.
    item["echo_prompt"] = (
        f"Use /assisted-apply for workflow {item['workflow_id']}.{attempt} "
        "This is the launcher's automatic repeat of the opening message, typed "
        "before the run began. It carries no new instruction: do not spawn, "
        "kill or restart a subagent and do not send a status because of it. "
        "Never submit the application or mark it completed."
    )
    item["handoff_messages"] = [item["primary_prompt"], item["echo_prompt"]]
    return item
