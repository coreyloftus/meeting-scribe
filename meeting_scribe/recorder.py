"""Start/stop the two capture processes and track session state.

We deliberately capture system audio and mic into TWO separate files at their
own native sample rates:

  * system  -> ScreenCaptureKit helper (bin/syscap)
  * mic      -> ffmpeg avfoundation

They are resampled INDEPENDENTLY later (see audio.py). Nothing is ever joined
at mismatched rates, so the old "underwater / half-speed" bug cannot recur.
Keeping them separate is also what gives us Me/Them speaker labels for free.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .config import Config, REPO_ROOT

STATE_DIR = Path.home() / ".local" / "state" / "meeting-scribe"
SESSION_FILE = STATE_DIR / "session.json"
HELPER_BIN = REPO_ROOT / "bin" / "syscap"


class RecorderError(Exception):
    pass


@dataclass
class Session:
    started_at: str
    base: str               # path stem shared by all artifacts
    system_wav: str | None
    mic_wav: str | None
    system_pid: int | None
    mic_pid: int | None

    @property
    def base_path(self) -> Path:
        return Path(self.base)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    # When the caller is the process's parent (the daemon spawns the captures
    # and stays alive), an exited child lingers as a zombie that still answers
    # kill(pid, 0) — reap it first or stop() waits its full timeout on a corpse.
    # In the CLI case the captures aren't our children and waitpid raises.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def load_session() -> Session | None:
    if not SESSION_FILE.is_file():
        return None
    try:
        return Session(**json.loads(SESSION_FILE.read_text()))
    except Exception:
        return None


def _save_session(s: Session) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(asdict(s), indent=2))


def _clear_session() -> None:
    SESSION_FILE.unlink(missing_ok=True)


def is_recording() -> bool:
    s = load_session()
    return bool(s and (_alive(s.system_pid) or _alive(s.mic_pid)))


def default_mic_device(cfg: Config) -> str:
    if cfg.mic_device:
        return cfg.mic_device
    # Ask switchaudio for the current default input; fall back to a safe name.
    try:
        out = subprocess.run(["SwitchAudioSource", "-c", "-t", "input"],
                             capture_output=True, text=True, timeout=5)
        name = out.stdout.strip()
        if name:
            return name
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return "MacBook Pro Microphone"


def start(cfg: Config) -> Session:
    if is_recording():
        raise RecorderError("Already recording. Run `scribe stop` first.")
    _clear_session()

    rec_dir = cfg.recordings_dir
    rec_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base = rec_dir / stamp

    system_wav = mic_wav = None
    system_pid = mic_pid = None

    # --- system audio via ScreenCaptureKit helper --------------------------
    if cfg.capture_system_audio:
        if not HELPER_BIN.exists():
            raise RecorderError(
                f"System-audio helper not built: {HELPER_BIN}\n"
                f"Build it with:  bash scripts/build_helper.sh"
            )
        system_wav = str(base) + ".system.wav"
        log = open(str(base) + ".syscap.log", "wb")
        p = subprocess.Popen([str(HELPER_BIN), system_wav],
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        system_pid = p.pid

    # --- mic via ffmpeg avfoundation ---------------------------------------
    if cfg.capture_mic:
        mic = default_mic_device(cfg)
        mic_wav = str(base) + ".mic.wav"
        log = open(str(base) + ".ffmpeg.log", "wb")
        # No -ar here: capture at the device's native rate, resample later.
        # -flush_packets 1: write every packet straight to disk — ffmpeg 8's
        # stop path only flushes whole buffered chunks, which otherwise drops
        # the last few seconds of the meeting.
        p = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-f", "avfoundation", "-i", f":{mic}",
             "-ac", "1", "-flush_packets", "1", "-y", mic_wav],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        mic_pid = p.pid

    if not system_pid and not mic_pid:
        raise RecorderError("Both capture_system_audio and capture_mic are disabled — nothing to record.")

    session = Session(
        started_at=datetime.now().isoformat(timespec="seconds"),
        base=str(base), system_wav=system_wav, mic_wav=mic_wav,
        system_pid=system_pid, mic_pid=mic_pid,
    )
    _save_session(session)

    # Give the processes a beat and check they didn't die instantly
    # (bad device name, permission denied, etc.).
    time.sleep(1.0)
    if system_pid and not _alive(system_pid):
        raise RecorderError(
            "System-audio helper exited immediately. Most likely the terminal "
            "lacks Screen Recording permission.\n"
            "Grant it in System Settings > Privacy & Security > Screen Recording, "
            f"then retry. Log: {base}.syscap.log")
    if mic_pid and not _alive(mic_pid):
        raise RecorderError(
            f"Mic capture (ffmpeg) exited immediately. Check the input device. "
            f"Log: {base}.ffmpeg.log")

    return session


def _stop_pid(pid: int | None, timeout: float = 10.0) -> None:
    """TERM, TERM again, then KILL. The double TERM is deliberate: Homebrew
    ffmpeg 8 never acts on its first signal while capturing from avfoundation
    (the graceful-stop flag isn't polled), but the second signal takes its
    force-exit path, which still writes the WAV trailer ("Exiting normally,
    received signal 15"). syscap exits cleanly on the first TERM."""
    if not _alive(pid):
        return
    for sig, wait in ((signal.SIGTERM, 0.6),
                      (signal.SIGTERM, max(timeout - 2.6, 2.0)),
                      (signal.SIGKILL, 2.0)):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        deadline = time.time() + wait
        while time.time() < deadline:
            if not _alive(pid):
                return
            time.sleep(0.1)


def stop(cfg: Config) -> Session:
    s = load_session()
    if s is None:
        raise RecorderError("No recording in progress.")
    _stop_pid(s.mic_pid)
    _stop_pid(s.system_pid)
    _clear_session()
    return s
