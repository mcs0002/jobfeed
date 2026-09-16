"""Single source of truth for the two-message Antigravity handoff."""
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from urllib.parse import urlencode, urlparse

# The helper is invoked by absolute path so the command text is identical no
# matter what directory Antigravity happens to be in.
ROOT_HINT = str(Path(__file__).resolve().parent)


def safe_application_url(value: str) -> str:
    value = (value or "").strip()
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and bool(parsed.netloc) else ""


def workflow_token(secret: str, workflow_id: str) -> str:
    payload = f"application-workflow:{workflow_id}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def handoff_view(workflow: dict, secret: str, public_base_url: str,
                 offer_elsewhere: bool | None = None) -> dict:
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
        f"{ROOT_HINT}/.venv/bin/python -B {ROOT_HINT}/application_status.py "
        "--fixed-request-file"
    )
    item["followup_prompt"] = (
        f"Use /assisted-apply for workflow {item['workflow_id']}. "
        f"To report status, write {item['agent_status_request_file']} as "
        f'{{"workflow_id": "{item["workflow_id"]}", "status": STATE, "detail": "SHORT NOTE"}} '
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
    # Antigravity queues a message typed while a turn is running and delivers it
    # only when that turn ends. On 2026-09-12 the whole Trafigura application ran
    # inside the first turn, so the context arrived at step 97 of 105 and framed
    # nothing. The first message therefore has to be self-sufficient.
    item["primary_prompt"] = f"{item['browser_command']}\n\n{item['followup_prompt']}"
    # The second message stays: it re-states "never submit" once the work is done,
    # and it keeps the helper's messages[1] prefix check satisfied without a
    # recompile, which would cost another Accessibility re-grant.
    item["handoff_messages"] = [item["primary_prompt"], item["followup_prompt"]]
    return item
