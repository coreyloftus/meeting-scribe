"""Transcribe with whisper.cpp (whisper-cli), per channel, with speaker labels.

Because mic and system audio were recorded to separate files, we can transcribe
each one independently and tag every segment with WHO was speaking, then weave
them back together in time order into a "Me: … / Them: …" dialogue.
"""
from __future__ import annotations

import array
import json
import math
import re
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from . import audio


class TranscribeError(Exception):
    pass

# A pause this long inside one speaker's run starts a new dialogue line, so a
# channel that never alternates (e.g. the other channel was silent) still
# renders as readable time-segmented lines instead of one giant paragraph.
# Continuous speech (a speakerphone call heard entirely by the mic) can go
# minutes without such a pause, so a line is also capped by duration and broken
# at the next segment boundary.
GAP_BREAK_MS = 2000
MAX_LINE_MS = 30_000

# Echo dedup: two segments on opposite channels are the same utterance when
# their time ranges overlap (with slack for whisper timestamp drift between
# channels) and their word containment clears the similarity bar.
ECHO_SLACK_MS = 2000
ECHO_MIN_WORDS = 3
ECHO_SIMILARITY = 0.6


@dataclass
class Segment:
    start_ms: int
    end_ms: int
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
        offsets = item.get("offsets", {})
        start = int(offsets.get("from", 0))
        end = int(offsets.get("to", start))
        segs.append(Segment(start_ms=start, end_ms=end, text=text, speaker=speaker))
    return segs


def _whisper_wav_path(workdir: Path, speaker: str) -> Path:
    return workdir / f"{speaker.lower()}.16k.wav"


def transcribe_channel(cfg: Config, src_wav: Path, speaker: str, workdir: Path) -> list[Segment]:
    """Resample a single channel to 16 kHz mono and transcribe it."""
    whisper_wav = _whisper_wav_path(workdir, speaker)
    audio.to_whisper_wav(src_wav, whisper_wav)
    out_base = workdir / speaker.lower()
    _run_whisper(cfg, whisper_wav, out_base)
    return _parse_segments(out_base.with_suffix(".json"), speaker)


def transcribe(cfg: Config, system_wav: Path | None, mic_wav: Path | None) -> tuple[str, list[str]]:
    """Return (transcript, warnings), speaker-labelled when possible.

    Warnings flag conditions that make the transcript less trustworthy than it
    looks — a channel that produced no speech (so every line carries one
    label), or cross-channel echo that had to be deduplicated.
    """
    with tempfile.TemporaryDirectory(prefix="scribe-tx-") as tmp:
        workdir = Path(tmp)
        warnings: list[str] = []

        mic_ok = mic_wav and Path(mic_wav).is_file()
        sys_ok = system_wav and Path(system_wav).is_file()

        if cfg.speaker_labels and mic_ok and sys_ok:
            mic_segs = transcribe_channel(cfg, Path(mic_wav), cfg.me_label, workdir)
            sys_segs = transcribe_channel(cfg, Path(system_wav), cfg.them_label, workdir)

            if mic_segs and sys_segs:
                mic_segs, sys_segs, dropped = _dedupe_echoes(
                    mic_segs, sys_segs,
                    _whisper_wav_path(workdir, cfg.me_label),
                    _whisper_wav_path(workdir, cfg.them_label))
                if dropped:
                    warnings.append(
                        f"Cross-channel echo detected — {dropped} duplicated segments "
                        f"removed (each utterance kept on the channel where it was loudest). "
                        f"Speaker labels may be imperfect where the echo was strong.")
            else:
                for empty_label, segs, full_label in (
                        (cfg.them_label, sys_segs, cfg.me_label),
                        (cfg.me_label, mic_segs, cfg.them_label)):
                    if not segs:
                        warnings.append(
                            f"The {empty_label!r} channel produced no speech — every line "
                            f"is labeled {full_label!r}, so speaker attribution is "
                            f"unreliable (both voices may be on one channel).")
                if not mic_segs and not sys_segs:
                    raise TranscribeError(
                        "Neither audio channel produced any speech — check the "
                        "recordings and whisper model.")

            segments = sorted(mic_segs + sys_segs, key=lambda s: s.start_ms)
            return _render_dialogue(segments), warnings

        # Single-channel (or labels off): just transcribe whatever we have.
        src = Path(mic_wav) if mic_ok else Path(system_wav) if sys_ok else None
        if src is None:
            raise TranscribeError("No audio files to transcribe.")
        speaker = cfg.me_label if mic_ok else cfg.them_label
        segments = transcribe_channel(cfg, src, speaker, workdir)
        return "\n".join(s.text for s in segments).strip(), warnings


_WORD = re.compile(r"[a-z0-9']+")


def _echo_similarity(a: str, b: str) -> float:
    """Word containment: how much of the shorter segment appears in the other.

    Containment (not symmetric similarity) because whisper splits the two
    channels at different points — one channel's segment often covers half of
    two segments on the other channel.
    """
    wa = set(_WORD.findall(a.lower()))
    wb = set(_WORD.findall(b.lower()))
    if min(len(wa), len(wb)) < ECHO_MIN_WORDS:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _segment_energies(wav_path: Path, segs: list[Segment]) -> list[float]:
    """Per-segment RMS on a 16 kHz mono PCM wav, relative to the channel's own
    median. Relative because the two channels have unrelated gain — an echo is
    quiet *for its channel*, not necessarily quieter than the other channel.
    Returns all 1.0 on any read failure (dedup then ties toward the mic).
    """
    try:
        with wave.open(str(wav_path), "rb") as w:
            rate = w.getframerate()
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return [1.0] * len(segs)
            pcm = array.array("h")
            pcm.frombytes(w.readframes(w.getnframes()))
    except (OSError, wave.Error):
        return [1.0] * len(segs)
    rms: list[float] = []
    for s in segs:
        lo = max(0, int(s.start_ms * rate / 1000))
        hi = min(len(pcm), int(s.end_ms * rate / 1000))
        chunk = pcm[lo:hi]
        if not chunk:
            rms.append(0.0)
            continue
        rms.append(math.sqrt(sum(x * x for x in chunk) / len(chunk)))
    med = sorted(rms)[len(rms) // 2] if rms else 0.0
    if med <= 0:
        return [1.0] * len(segs)
    return [e / med for e in rms]


def _dedupe_echoes(mic_segs: list[Segment], sys_segs: list[Segment],
                   mic_wav: Path, sys_wav: Path) -> tuple[list[Segment], list[Segment], int]:
    """Drop cross-channel echoes: when both channels heard the same utterance
    (mic picking up the speakers, or the meeting app returning your voice),
    keep the copy on the channel where it was relatively loudest — that's the
    channel the utterance actually belongs to.
    """
    mic_rel = _segment_energies(mic_wav, mic_segs)
    sys_rel = _segment_energies(sys_wav, sys_segs)
    drop_mic: set[int] = set()
    drop_sys: set[int] = set()
    for i, a in enumerate(mic_segs):
        for j, b in enumerate(sys_segs):
            if b.start_ms > a.end_ms + ECHO_SLACK_MS:
                break  # sys_segs is time-ordered; nothing later can overlap
            if b.end_ms < a.start_ms - ECHO_SLACK_MS:
                continue
            if _echo_similarity(a.text, b.text) < ECHO_SIMILARITY:
                continue
            if mic_rel[i] >= sys_rel[j]:
                drop_sys.add(j)
            else:
                drop_mic.add(i)
    kept_mic = [s for i, s in enumerate(mic_segs) if i not in drop_mic]
    kept_sys = [s for j, s in enumerate(sys_segs) if j not in drop_sys]
    return kept_mic, kept_sys, len(drop_mic) + len(drop_sys)


def _render_dialogue(segments: list[Segment]) -> str:
    """Collapse consecutive same-speaker segments into one labelled line,
    breaking the run at long pauses so a single hot channel still reads as
    dialogue rather than one multi-minute paragraph."""
    lines: list[str] = []
    cur_speaker: str | None = None
    buf: list[str] = []
    last_end: int | None = None
    line_start = 0
    for s in segments:
        gap = last_end is not None and s.start_ms - last_end > GAP_BREAK_MS
        too_long = buf and s.end_ms - line_start > MAX_LINE_MS
        if s.speaker != cur_speaker or gap or too_long:
            if buf:
                lines.append(f"{cur_speaker}: {' '.join(buf)}")
            cur_speaker = s.speaker
            buf = [s.text]
            line_start = s.start_ms
        else:
            buf.append(s.text)
        last_end = s.end_ms
    if buf:
        lines.append(f"{cur_speaker}: {' '.join(buf)}")
    return "\n".join(lines).strip()
