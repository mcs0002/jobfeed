"""Type a recruiting password into Chrome without it entering model context.

`application_account.py` used to hand the generated password back as JSON so
the browser agent could type it. That put a live credential into the Antigravity
trajectory, which is stored server-side: four occurrences on the first live
credential run, Gunvor 2026-09-12.

The password now stays on the machine. The agent focuses the password field and
runs one fixed command; this reads the value out of Keychain and types it over
the Chrome DevTools Protocol. The value is never printed, never returned and
never logged.

Three checks run before a single character is typed, because typing a password
into the wrong place is the failure that matters:

1. the page being typed into must be on the host the workflow's application URL
   names;
2. the focused element must be a password input;
3. the length is read back afterwards, never the value.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets as _secrets
import socket
import struct
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from application_handoff import safe_application_url
from db import JobDB

ROOT = Path(__file__).resolve().parent
FIXED_REQUEST = Path("/private/tmp/the user-application-password-request.json")
DEVTOOLS_PORT_FILE = (Path.home() / "Library" / "Application Support" / "Google"
                      / "Chrome" / "DevToolsActivePort")
WORKFLOW_RE = re.compile(r"^apply_[a-f0-9]{8,64}$")


class MiniSocket:
    """The few RFC 6455 frames a CDP conversation needs, over loopback.

    Hand-rolled rather than adding a dependency: both ends are ours, the
    transport is localhost, and the messages are small JSON commands. Chrome
    answers /json with 404 on this build, so the browser endpoint comes from
    DevToolsActivePort instead of the discovery API.
    """

    def __init__(self, host: str, port: int, path: str, timeout: float = 15.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(_secrets.token_bytes(16)).decode()
        self.sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("devtools closed during handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"devtools refused upgrade: {head[:80]!r}")
        self.buffer = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, count: int) -> bytes:
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("devtools closed")
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        header = bytearray([0x81])
        mask = _secrets.token_bytes(4)
        size = len(data)
        if size < 126:
            header.append(0x80 | size)
        elif size < 1 << 16:
            header.append(0x80 | 126); header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127); header += struct.pack(">Q", size)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def receive(self) -> dict:
        while True:
            first, second = self._read(2)
            opcode, size = first & 0x0F, second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read(8))[0]
            body = self._read(size) if size else b""
            if opcode == 0x8:
                raise ConnectionError("devtools closed")
            if opcode in (0x9, 0xA):
                continue
            return json.loads(body)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Devtools:
    def __init__(self) -> None:
        lines = DEVTOOLS_PORT_FILE.read_text().splitlines()
        if len(lines) < 2:
            raise RuntimeError("Chrome is not listening for DevTools")
        self.socket = MiniSocket("127.0.0.1", int(lines[0].strip()), lines[1].strip())
        self.next_id = 0

    def call(self, method: str, params: dict | None = None,
             session: str | None = None) -> dict:
        self.next_id += 1
        message: dict = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.socket.send(message)
        while True:
            reply = self.socket.receive()
            if reply.get("id") != self.next_id:
                continue
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error'].get('message', '')}")
            return reply.get("result", {})

    def close(self) -> None:
        self.socket.close()


def _keychain(service: str, account: str) -> str | None:
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", account,
         "-s", service, "-w"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _expected(workflow_id: str) -> tuple[str, str, str]:
    """(host, keychain service, profile email) for this workflow."""
    if not WORKFLOW_RE.fullmatch(workflow_id):
        raise ValueError("invalid workflow id")
    db = JobDB(os.environ.get("JOBS_DB", str(ROOT / "jobs.db")))
    try:
        workflow = db.get_application_workflow(workflow_id)
        if workflow is None or workflow["status"] == "completed":
            raise ValueError("workflow unavailable")
        url = safe_application_url(workflow["application_url"])
        if not url:
            raise ValueError("workflow unavailable")
        host = (urlparse(url).hostname or "").lower()
        row = db.conn.execute(
            "SELECT credential_service FROM application_accounts WHERE workflow_id=?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise ValueError("no recruiting account prepared for this workflow")
        return host, row[0], _profile_email()
    finally:
        db.conn.close()


def _profile_email() -> str:
    profile = json.loads((ROOT / "secrets" / "applicant_profile.json").read_text())
    email = str(profile.get("email", "")).strip()
    if not email:
        raise ValueError("profile has no email")
    return email


# Returns only booleans and a length. Reading the field's value back into this
# process would put the credential exactly where it is being kept out of.
_FOCUS_PROBE = """(() => {
  const el = document.activeElement;
  if (!el) return {focused: false};
  return {
    focused: true,
    isPassword: el.tagName === 'INPUT' && el.type === 'password',
    length: typeof el.value === 'string' ? el.value.length : -1,
  };
})()"""


def fill(workflow_id: str) -> int:
    host, service, email = _expected(workflow_id)
    password = _keychain(service, email)
    if not password:
        print("no stored password for this site", file=sys.stderr)
        return 1
    devtools = Devtools()
    try:
        targets = [
            t for t in devtools.call("Target.getTargets").get("targetInfos", [])
            if t.get("type") == "page"
            and (urlparse(t.get("url", "")).hostname or "").lower() == host
        ]
        if len(targets) != 1:
            print(f"expected exactly one open page on {host}, found {len(targets)}",
                  file=sys.stderr)
            return 1
        session = devtools.call(
            "Target.attachToTarget",
            {"targetId": targets[0]["targetId"], "flatten": True},
        )["sessionId"]
        probe = devtools.call(
            "Runtime.evaluate", {"expression": _FOCUS_PROBE, "returnByValue": True},
            session=session,
        )["result"]["value"]
        if not probe.get("focused") or not probe.get("isPassword"):
            print("the focused element is not a password field", file=sys.stderr)
            return 1
        devtools.call("Input.insertText", {"text": password}, session=session)
        after = devtools.call(
            "Runtime.evaluate", {"expression": _FOCUS_PROBE, "returnByValue": True},
            session=session,
        )["result"]["value"]
        if after.get("length") != len(password):
            print("the field did not take the whole value", file=sys.stderr)
            return 1
    finally:
        devtools.close()
    print(json.dumps({"status": "filled", "host": host,
                      "characters": len(password)}, separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Type the stored recruiting password.")
    parser.add_argument("--fixed-request-file", action="store_true")
    args = parser.parse_args()
    if not args.fixed_request_file:
        parser.error("--fixed-request-file is the only supported mode")
    try:
        request = json.loads(FIXED_REQUEST.read_text())
        if not isinstance(request, dict) or set(request) != {"workflow_id"}:
            raise ValueError("request must hold only workflow_id")
        return fill(str(request["workflow_id"]))
    except (OSError, ValueError, RuntimeError, ConnectionError,
            json.JSONDecodeError) as exc:
        print(f"password fill failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
