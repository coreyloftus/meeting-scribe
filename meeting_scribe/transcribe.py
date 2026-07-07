"""Transcribe with whisper.cpp (whisper-cli), per channel, with speaker labels.

Because mic and system audio were recorded to separate files, we can transcribe
each one independently and tag every segment with WHO was speaking, then weave
them back together in time order into a "Me: … / Them: …" dialogue.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from . import audio


class TranscribeError(Exception):
    pass


@dataclass
class Segment:
    start_ms: int
    text: str
    speaker: str


def _run_whisper(cfg: Config, wav: Path, out_base: Path) -> None:
    model = cfg.whisper_model
    if not model or not Path(model).is_file():
        raise TranscribeError(
            f"whisper model not found: {model}\n"
            f"Download one, e.g. ggml-base.en.bin, and set transcription.whisper_model.")
    cmd = [
        cfg.whisper_cli,
        "--model", str(model),
        "--output-json",
        "--output-file", str(out_base),
        "--threads", str(cfg.whisper_threads),
        # Never condition a 30s window on the previous window's text. With
        # context carry-over, one bad window (usually a long silence) poisons
        # every window after it: the decoder locks into repeating a phrase or
        # emitting [BLANK_AUDIO] for the rest of the file, even over clear
        # speech. Costs a little cross-sentence continuity; worth it.
        "--max-context", "0",
    ]
    vad = cfg.vad_model
    if vad and Path(vad).is_file():
        # Skip non-speech before decoding. A meeting channel is often >50%
        # silence, and decoding silence is where hallucinations start.
        cmd += ["--vad", "--vad-model", str(vad)]
    if cfg.language and cfg.language != "auto":
        cmd += ["--language", cfg.language]
    cmd.append(str(wav))
    try:
        subprocess.run(cmd, capture_output=True, check=True, text=True)
    except FileNotFoundError as e:
        raise TranscribeError(f"whisper-cli not found ({cfg.whisper_cli}). Is whisper.cpp installed?") from e
    except subprocess.CalledProcessError as e:
        raise TranscribeError(f"whisper failed: {e.stderr or e.stdout}") from e


# Whisper annotates non-speech as bracketed/parenthesized markers —
# [BLANK_AUDIO], [ Silence ], (mouse clicks), ♪ — not words anyone said.
_NON_SPEECH = re.compile(r"[\[(][^\])]*[\])]|♪")


def _parse_segments(json_path: Path, speaker: str) -> list[Segment]:
    if not json_path.is_file():
        return []
    data = json.loads(json_path.read_text())
    segs = []
    for item in data.get("transcription", []):
        text = _NON_SPEECH.sub("", item.get("text") or "")
        text = re.sub(r"\s{2,}", " ", text).strip()
        if not text:
            continue
        start = int(item.get("offsets", {}).get("from", 0))
        segs.append(Segment(start_ms=start, text=text, speaker=speaker))
    return segs


def transcribe_channel(cfg: Config, src_wav: Path, speaker: str, workdir: Path) -> list[Segment]:
    """Resample a single channel to 16 kHz mono and transcribe it."""
    whisper_wav = workdir / f"{speaker.lower()}.16k.wav"
    audio.to_whisper_wav(src_wav, whisper_wav)
    out_base = workdir / speaker.lower()
    _run_whisper(cfg, whisper_wav, out_base)
    return _parse_segments(out_base.with_suffix(".json"), speaker)


def transcribe(cfg: Config, system_wav: Path | None, mic_wav: Path | None) -> str:
    """Return a single transcript string, speaker-labelled when possible."""
    with tempfile.TemporaryDirectory(prefix="scribe-tx-") as tmp:
        workdir = Path(tmp)
        segments: list[Segment] = []

        mic_ok = mic_wav and Path(mic_wav).is_file()
        sys_ok = system_wav and Path(system_wav).is_file()

        if cfg.speaker_labels and mic_ok and sys_ok:
            segments += transcribe_channel(cfg, Path(mic_wav), cfg.me_label, workdir)
            segments += transcribe_channel(cfg, Path(system_wav), cfg.them_label, workdir)
            segments.sort(key=lambda s: s.start_ms)
            return _render_dialogue(segments)

        # Single-channel (or labels off): just transcribe whatever we have.
        src = Path(mic_wav) if mic_ok else Path(system_wav) if sys_ok else None
        if src is None:
            raise TranscribeError("No audio files to transcribe.")
        speaker = cfg.me_label if mic_ok else cfg.them_label
        segments = transcribe_channel(cfg, src, speaker, workdir)
        return "\n".join(s.text for s in segments).strip()


def _render_dialogue(segments: list[Segment]) -> str:
    """Collapse consecutive same-speaker segments into one labelled line."""
    lines: list[str] = []
    cur_speaker: str | None = None
    buf: list[str] = []
    for s in segments:
        if s.speaker != cur_speaker:
            if buf:
                lines.append(f"{cur_speaker}: {' '.join(buf)}")
            cur_speaker = s.speaker
            buf = [s.text]
        else:
            buf.append(s.text)
    if buf:
        lines.append(f"{cur_speaker}: {' '.join(buf)}")
    return "\n".join(lines).strip()
