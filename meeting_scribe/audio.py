"""ffmpeg helpers: probe, resample to whisper's 16 kHz mono, level checks.

Every conversion here is an INDEPENDENT, pitch-preserving resample. This is the
fix for the original bug, where two avfoundation inputs at 48 kHz and 24 kHz
were fed into a `join` filter that does not resample — so the 48 kHz side got
clocked at 24 kHz and played back at half speed, an octave low ("underwater").
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

WHISPER_RATE = 16000


def probe_sample_rate(path: Path) -> int | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15)
        data = json.loads(out.stdout or "{}")
        return int(data["streams"][0]["sample_rate"])
    except Exception:
        return None


def to_whisper_wav(src: Path, dst: Path) -> Path:
    """Resample any input to 16 kHz mono PCM — the format whisper.cpp wants."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", str(src),
         "-ac", "1", "-ar", str(WHISPER_RATE), str(dst)],
        capture_output=True, check=True)
    return dst


def mean_volume_db(path: Path) -> float | None:
    """Mean volume in dBFS via ffmpeg volumedetect. None on failure."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", str(path), "-af", "volumedetect",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=120)
    except subprocess.SubprocessError:
        return None
    for line in res.stderr.splitlines():
        if "mean_volume:" in line:
            try:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip())
            except ValueError:
                return None
    return None


def is_silent(db: float | None, threshold: float = -55.0) -> bool:
    if db is None:
        return False
    return db < threshold


def looks_like_lowquality_bluetooth(mic_wav: Path) -> int | None:
    """If the mic was captured below ~32 kHz it is almost certainly a Bluetooth
    headset stuck in hands-free (HFP) mode — low quality. Returns the offending
    sample rate so the caller can warn, or None if it looks fine.
    """
    rate = probe_sample_rate(mic_wav)
    if rate is not None and rate < 32000:
        return rate
    return None
