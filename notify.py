#!/usr/bin/env python3
"""
Tiny email-alert helper. Sends a short plaintext alert via SMTP SSL.

Configuration (all via env / .env — no in-code personal defaults):
    NOTIFY_EMAIL             Sender and recipient address (required to send).
    SMTP_HOST                SMTP server hostname (required to send).
    SMTP_PASSWORD            SMTP password (preferred; works under launchd).
                             LEGACY_MAIL_PASSWORD is accepted as a legacy fallback.
    SMTP_KEYCHAIN_SERVICE    macOS Keychain service name for the SMTP password
                             (optional; only tried if set; dev Mac fallback).

If NOTIFY_EMAIL or SMTP_HOST is unset, send_alert prints a one-line warning
and returns False without raising — callers (selfcheck) must not crash.

Sending mail must NEVER crash the caller: every path is wrapped in try/except
and returns a bool. On failure it prints a WARN to stderr and returns False.

CLI:
    python notify.py --test            # one canned test message
    python notify.py "subject" "body"  # arbitrary alert
"""
import os
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent
SMTP_PORT = 465
# Canonical password env var, with a legacy fallback tried silently so existing
# deployments that set the old personal name keep working unchanged.
ENV_VAR = "SMTP_PASSWORD"
ENV_VAR_FALLBACK = "LEGACY_MAIL_PASSWORD"
SUBJECT_PREFIX = "[job-scraper] "


def _load_dotenv() -> None:
    """Fold the project .env into os.environ (setdefault), mirroring main.py's
    loader so notify works standalone (selfcheck imports notify without importing
    main). Headless production hosts keep secrets in .env, not in the Keychain,
    because launchd agents cannot unlock the login Keychain at scan time."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _get_password() -> str | None:
    """Resolve the SMTP password. Order: env var → .env → macOS Keychain.

    The env/.env path is the production (headless M1) source: a launchd agent's
    login Keychain is locked at scan time, so the Keychain lookup there fails
    with 'not found'/'interaction not allowed'. The Keychain fallback (via
    SMTP_KEYCHAIN_SERVICE) still serves a dev Mac where the credential is stored
    in the Keychain. None when every source is exhausted."""
    pw = os.environ.get(ENV_VAR) or os.environ.get(ENV_VAR_FALLBACK)
    if pw:
        return pw
    _load_dotenv()
    pw = os.environ.get(ENV_VAR) or os.environ.get(ENV_VAR_FALLBACK)
    if pw:
        return pw

    keychain_service = os.environ.get("SMTP_KEYCHAIN_SERVICE", "")
    if not keychain_service:
        # Keychain lookup disabled: SMTP_KEYCHAIN_SERVICE not set.
        return None
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", keychain_service, "-w"],
            capture_output=True, text=True,
        )
    except Exception as e:
        print(f"WARN: notify: keychain lookup raised {e!r}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(
            f"WARN: notify: no {ENV_VAR} in env/.env and keychain lookup failed "
            f"(exit {proc.returncode}) for service {keychain_service!r}",
            file=sys.stderr,
        )
        return None
    pw = proc.stdout.strip()
    if not pw:
        print("WARN: notify: keychain returned an empty password",
              file=sys.stderr)
        return None
    return pw


def send_alert(subject: str, body: str) -> bool:
    """Send a short plaintext alert. Returns True on success, False otherwise.

    Never raises: any failure (unconfigured env, no password, SMTP error, …)
    is caught, logged as a WARN to stderr, and reported as False so callers
    can ignore it.
    """
    try:
        _load_dotenv()
        addr = os.environ.get("NOTIFY_EMAIL", "")
        smtp_host = os.environ.get("SMTP_HOST", "")
        if not addr or not smtp_host:
            print(
                "WARN: notify: NOTIFY_EMAIL and SMTP_HOST must be set — alert not sent",
                file=sys.stderr,
            )
            return False

        password = _get_password()
        if password is None:
            return False

        msg = EmailMessage()
        msg["Subject"] = SUBJECT_PREFIX + subject
        msg["From"] = addr
        msg["To"] = addr
        msg.set_content(body)

        with smtplib.SMTP_SSL(smtp_host, SMTP_PORT, timeout=30) as smtp:
            smtp.login(addr, password)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"WARN: notify: failed to send alert: {e!r}", file=sys.stderr)
        return False


def main(argv: list[str]) -> int:
    if len(argv) == 1 and argv[0] == "--test":
        ok = send_alert("test alert", "This is a test alert from notify.py.")
        print("OK" if ok else "FAIL")
        return 0 if ok else 1
    if len(argv) == 2:
        ok = send_alert(argv[0], argv[1])
        print("OK" if ok else "FAIL")
        return 0 if ok else 1
    print(
        'usage: notify.py --test | notify.py "subject" "body"',
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
