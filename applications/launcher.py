"""Narrow, local IPC backend for the Jobfeed Antigravity launcher."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path

from applications.handoff import handoff_view, safe_application_url
from jobfeed.envfile import load_dotenv
from jobfeed.db import JobDB

ROOT = Path(__file__).resolve().parent.parent
SUPPORT = Path.home() / "Library" / "Application Support" / "Jobfeed"
QUEUE = SUPPORT / "application-launcher-queue"
ID_RE = re.compile(r"^(?:apply|launch)_[a-f0-9]{8,64}$")




def _secure_queue() -> None:
    SUPPORT.mkdir(mode=0o700, parents=True, exist_ok=True)
    QUEUE.mkdir(mode=0o700, exist_ok=True)
    for path in (SUPPORT, QUEUE):
        if path.is_symlink():
            raise ValueError("symlink queue directory refused")
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            os.chmod(path, 0o700)


def enqueue_launch(workflow: dict) -> Path:
    _secure_queue()
    workflow_id = workflow["workflow_id"]
    request_id = workflow["launch_request_id"]
    if not ID_RE.fullmatch(workflow_id) or not ID_RE.fullmatch(request_id):
        raise ValueError("invalid launch identifiers")
    path = QUEUE / f"{request_id}.json"
    if path.exists() or path.is_symlink():
        raise FileExistsError("launch request already exists")
    payload = {"version": 1, "workflow_id": workflow_id, "launch_request_id": request_id}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fp:
        json.dump(payload, fp, separators=(",", ":"))
    return path


def _read_request(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("symlink request refused")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("unsafe request permissions")
    obj = json.loads(path.read_text())
    if set(obj) != {"version", "workflow_id", "launch_request_id"} or obj["version"] != 1:
        raise ValueError("invalid request shape")
    if not ID_RE.fullmatch(str(obj["workflow_id"])) or not ID_RE.fullmatch(str(obj["launch_request_id"])):
        raise ValueError("invalid request identifiers")
    return obj


def _db() -> JobDB:
    path = os.environ.get("JOBS_DB", str(ROOT / "jobs.db"))
    return JobDB(path)


def _payload(workflow: dict, *, require_run: bool = False) -> dict:
    secret = os.environ.get("WEB_SECRET", "")
    public = os.environ.get("WEB_PUBLIC_BASE_URL", "").rstrip("/")
    if not secret or not public or not safe_application_url(workflow["application_url"]):
        raise ValueError("launcher configuration invalid")
    db = _db()
    try:
        offer = db.offer_elsewhere(workflow["job_id"])
        prior = db.prior_applications(workflow["job_id"])
        run = db.open_application_run(workflow["workflow_id"])
    finally:
        db.conn.close()
    if require_run and (run is None or run["state"] not in {"launching", "running"}):
        raise ValueError("launch has no active application attempt")
    if run is not None:
        workflow = dict(workflow, run_id=run["run_id"])
    view = handoff_view(workflow, secret, public, offer_elsewhere=offer,
                        prior_applications=prior)
    return {
        "version": 1,
        "workflow_id": view["workflow_id"],
        "launch_request_id": view["launch_request_id"],
        "messages": view["handoff_messages"],
    }


def claim_next() -> int:
    _secure_queue()
    for path in sorted(QUEUE.glob("launch_*.json"), key=lambda p: p.stat().st_mtime):
        try:
            request = _read_request(path)
            db = _db()
            try:
                workflow = db.claim_application_launch(request["launch_request_id"])
            finally:
                db.conn.close()
            path.unlink(missing_ok=True)
            if workflow is not None and workflow["workflow_id"] == request["workflow_id"]:
                print(json.dumps(_payload(workflow, require_run=True), separators=(",", ":")))
                return 0
        except Exception:
            path.unlink(missing_ok=True)
    return 1


def finish(request_id: str, result: str, code: str) -> int:
    if not ID_RE.fullmatch(request_id) or result not in {"succeeded", "failed"}:
        return 2
    db = _db()
    try:
        db.finish_application_launch(request_id, result == "succeeded", code)
    finally:
        db.conn.close()
    return 0


def dry_run(workflow_id: str) -> int:
    if not ID_RE.fullmatch(workflow_id):
        return 2
    db = _db()
    try:
        workflow = db.get_application_workflow(workflow_id)
    finally:
        db.conn.close()
    if workflow is None:
        return 1
    print(json.dumps(_payload(workflow), separators=(",", ":")))
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--claim-next", action="store_true")
    group.add_argument("--finish", nargs=3, metavar=("REQUEST_ID", "RESULT", "ERROR_CODE"))
    group.add_argument("--dry-run-workflow")
    args = parser.parse_args()
    if args.claim_next:
        return claim_next()
    if args.finish:
        return finish(*args.finish)
    return dry_run(args.dry_run_workflow)


if __name__ == "__main__":
    raise SystemExit(main())
