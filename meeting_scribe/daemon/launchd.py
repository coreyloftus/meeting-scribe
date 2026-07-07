"""launchd LaunchAgent management for scribed.

`scribe daemon install` writes ~/Library/LaunchAgents/com.meetingscribe.scribed.plist
and bootstraps it; the agent keeps scribed running in the background and
restarts it on crash/login.

Note on PATH: launchd agents don't inherit a shell PATH, so the plist bakes in
Homebrew locations — ffmpeg/whisper-cli/SwitchAudioSource must be findable.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .state import LOG_FILE

LABEL = "com.meetingscribe.scribed"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def _scribed_argv() -> list[str]:
    exe = shutil.which("scribed")
    if exe:
        return [exe, "serve"]
    return [sys.executable, "-m", "meeting_scribe.daemon", "serve"]


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install() -> str:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": _scribed_argv(),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "EnvironmentVariables": {"PATH": _PATH},
    }
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_bytes(plistlib.dumps(plist))
    _launchctl("bootout", _domain(), str(PLIST_PATH))  # ignore failures; may not be loaded
    r = _launchctl("bootstrap", _domain(), str(PLIST_PATH))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
    return str(PLIST_PATH)


def uninstall() -> None:
    _launchctl("bootout", _domain(), str(PLIST_PATH))
    PLIST_PATH.unlink(missing_ok=True)


def start() -> None:
    r = _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
    if r.returncode != 0:
        raise RuntimeError(f"launchctl kickstart failed: {r.stderr.strip() or r.stdout.strip()}")


def stop() -> None:
    _launchctl("kill", "SIGTERM", f"{_domain()}/{LABEL}")


def status() -> str:
    if not PLIST_PATH.is_file():
        return "not installed"
    r = _launchctl("print", f"{_domain()}/{LABEL}")
    if r.returncode != 0:
        return "installed, not loaded"
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("state ="):
            return f"loaded ({line.split('=', 1)[1].strip()})"
    return "loaded"
