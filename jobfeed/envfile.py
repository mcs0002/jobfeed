"""Fold the project `.env` into `os.environ`, without python-dotenv.

Six modules had their own copy of this loader (main, notify, web/app,
application_status, application_account, application_launcher) and they had
drifted into two spellings of the quote stripping. It is one function now.

Headless production hosts keep secrets in `.env` rather than the Keychain,
because launchd agents cannot unlock the login Keychain at scan time — so this
runs on every entry point, and `setdefault` means a value already exported in
the environment always wins over the file.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def load_dotenv(path: Path | str | None = None) -> None:
    env_path = Path(path) if path is not None else ENV_PATH
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
