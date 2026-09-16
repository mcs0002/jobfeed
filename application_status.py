"""Report one workflow status with a command string that never varies.

Antigravity records a permission grant as the exact command text, so the curl
this replaced could never be reused: the token, the state and the free-text
detail were all in the string, making every status update a fresh prompt. Nine
variants had already accumulated in `globalPermissionGrants` after three
applications, and the list would have grown for the life of the machine.

The request goes in a fixed file instead, the same trick
`application_writer.py --fixed-request-file` already uses, so one grant covers
every future call. The token never reaches the agent at all now: it is derived
here from WEB_SECRET, which also keeps it out of shell history and out of the
grant list.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from application_handoff import workflow_token
from db import AGENT_WORKFLOW_STATES

ROOT = Path(__file__).resolve().parent
FIXED_REQUEST = Path("/private/tmp/the user-application-status-request.json")
ID_PREFIX = "apply_"


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _read_limit(raw) -> dict:
    """The optional application-limit observation, validated to its shape.

    `{"stated": true, "max": 1, "quote": "..."}` when the form or posting says
    how many applications are allowed, `{"stated": false}` when the run went
    through the whole form and it said nothing. The server applies the
    number-in-quote gate; this only refuses what cannot be one."""
    if not isinstance(raw, dict) or set(raw) - {"stated", "max", "quote"}:
        raise ValueError("application_limit holds only stated, max and quote")
    if raw.get("stated") is True:
        n = raw.get("max")
        quote = str(raw.get("quote") or "").strip()
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 20:
            raise ValueError("a stated application_limit needs an integer max")
        if len(quote) < 15:
            raise ValueError("a stated application_limit needs the verbatim sentence")
        return {"limit_verdict": "stated", "limit_max": str(n),
                "limit_quote": quote[:600]}
    if raw.get("stated") is False:
        return {"limit_verdict": "absent"}
    raise ValueError("application_limit.stated must be true or false")


def _read_request(path: Path) -> dict:
    obj = json.loads(path.read_text())
    allowed = {"workflow_id", "status", "detail", "application_limit"}
    if not isinstance(obj, dict) or set(obj) - allowed:
        raise ValueError(
            "request must hold only workflow_id, status, detail and application_limit")
    workflow_id = str(obj.get("workflow_id", ""))
    status = str(obj.get("status", ""))
    if not workflow_id.startswith(ID_PREFIX) or not workflow_id[6:].isalnum():
        raise ValueError("bad workflow id")
    if status not in AGENT_WORKFLOW_STATES:
        raise ValueError(
            f"status must be one of {', '.join(sorted(AGENT_WORKFLOW_STATES))}"
        )
    request = {"workflow_id": workflow_id, "status": status,
               "detail": str(obj.get("detail", ""))[:500]}
    if "application_limit" in obj:
        request.update(_read_limit(obj["application_limit"]))
    return request


def report(request: dict) -> int:
    secret = os.environ.get("WEB_SECRET", "")
    base = os.environ.get("WEB_PUBLIC_BASE_URL", "").rstrip("/")
    if not secret or not base:
        print("status reporting is not configured", file=sys.stderr)
        return 2
    fields = {
        "token": workflow_token(secret, request["workflow_id"]),
        "status": request["status"],
        "detail": request["detail"],
    }
    fields.update({k: v for k, v in request.items() if k.startswith("limit_")})
    payload = urllib.parse.urlencode(fields).encode()
    url = f"{base}/application/{request['workflow_id']}/agent/status"
    try:
        with urllib.request.urlopen(url, data=payload, timeout=20) as response:
            code, note = response.status, response.headers.get("X-Jobfeed-Message", "")
    except urllib.error.HTTPError as exc:
        code, note = exc.code, exc.headers.get("X-Jobfeed-Message", "")
    except OSError as exc:
        print(f"status callback failed: {exc}", file=sys.stderr)
        return 1
    # Print what the server said, not only that it said no. A refusal names the
    # rule it enforced, which is the difference between fixing the report and
    # reading the server's source to guess at it (BNP, 2026-09-16).
    print(f"{code} {note}".strip())
    return 0 if code == 200 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Report one workflow status.")
    parser.add_argument("--fixed-request-file", action="store_true",
                        help=f"read the request from {FIXED_REQUEST}")
    args = parser.parse_args()
    if not args.fixed_request_file:
        parser.error("--fixed-request-file is the only supported mode")
    _load_env()
    try:
        request = _read_request(FIXED_REQUEST)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"bad status request: {exc}", file=sys.stderr)
        return 2
    return report(request)


if __name__ == "__main__":
    raise SystemExit(main())
