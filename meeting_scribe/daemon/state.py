"""Daemon state files: bearer token, host:port discovery, log/db locations.

Everything lives in ~/.local/state/meeting-scribe/ next to session.json:
    daemon.token   random bearer token, chmod 600, regenerated if missing
    daemon.json    { host, port, pid, version } written on bind for discovery
    meetings.db    the SQLite index (db.py)
    scribed.log    daemon log
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from ..recorder import STATE_DIR

TOKEN_FILE = STATE_DIR / "daemon.token"
INFO_FILE = STATE_DIR / "daemon.json"
DB_FILE = STATE_DIR / "meetings.db"
LOG_FILE = STATE_DIR / "scribed.log"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48237


def ensure_token() -> str:
    """Read the bearer token, generating one (chmod 600) on first run."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.is_file():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    TOKEN_FILE.touch(mode=0o600, exist_ok=True)
    TOKEN_FILE.write_text(tok)
    os.chmod(TOKEN_FILE, 0o600)
    return tok


def read_token() -> str | None:
    if TOKEN_FILE.is_file():
        tok = TOKEN_FILE.read_text().strip()
        return tok or None
    return None


def write_info(host: str, port: int, version: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    INFO_FILE.write_text(json.dumps(
        {"host": host, "port": port, "pid": os.getpid(), "version": version}, indent=2))


def read_info() -> dict | None:
    if not INFO_FILE.is_file():
        return None
    try:
        return json.loads(INFO_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def base_url() -> str | None:
    """http://host:port for a (possibly) running daemon, from daemon.json."""
    info = read_info()
    if not info:
        return None
    return f"http://{info.get('host', DEFAULT_HOST)}:{info.get('port', DEFAULT_PORT)}"
