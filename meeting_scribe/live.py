"""Trailing live transcript while recording.

Tails the growing capture WAVs, cuts the new audio at a quiet moment every
~10 s, and transcribes each piece with Parakeet. This is a preview only: the
real transcript is still made from the finished files after Stop.
"""
from __future__ import annotations

import bisect
import struct
import subprocess
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from .config import Config
from .transcribe import (ECHO_SIMILARITY, GAP_BREAK_MS, MAX_LINE_MS, Segment,
                         _containment, _word_set)

RATE = 16000
POLL_SEC = 3.0
MIN_CHUNK_SEC = 8.0        # transcribe once a channel has this much new audio
CUT_SEARCH_SEC = 3.0       # look for the quietest point in the last N seconds
ECHO_WINDOW_MS = 10_000

_EMPTY = np.zeros(0, np.float32)


class _WavTail:
    """Reads the audio appended to a WAV that is still being written.

    The capture tools do not update the header sizes until they exit, so the
    header is used only to find the data offset and the sample format.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.offset: int | None = None
        self.fmt: tuple[int, int, int, int] | None = None   # channels, rate, align, bits

    def _parse_header(self, head: bytes) -> bool:
        pos = 12
        while pos + 8 <= len(head):
            cid = head[pos:pos + 4]
            size = struct.unpack("<I", head[pos + 4:pos + 8])[0]
            if cid == b"fmt ":
                _, ch, rate, _, align, bits = struct.unpack("<HHIIHH", head[pos + 8:pos + 24])
                self.fmt = (ch, rate, align, bits)
            elif cid == b"data":
                self.offset = pos + 8
                return self.fmt is not None
            pos += 8 + size + (size & 1)
        return False

    def read_new(self) -> np.ndarray:
        """New audio since the last call, as 16 kHz mono float32."""
        try:
            with open(self.path, "rb") as f:
                if self.offset is None and not self._parse_header(f.read(8192)):
                    return _EMPTY
                f.seek(self.offset)
                raw = f.read()
        except OSError:
            return _EMPTY
        ch, rate, align, bits = self.fmt
        raw = raw[:len(raw) // align * align]
        if not raw:
            return _EMPTY
        self.offset += len(raw)
        res = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error",
             "-f", "f32le" if bits == 32 else "s16le", "-ar", str(rate), "-ac", str(ch),
             "-i", "pipe:0", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "pipe:1"],
            input=raw, capture_output=True, check=True)
        return np.frombuffer(res.stdout, np.float32)


def _quiet_cut(audio: np.ndarray) -> int:
    """Sample index of the quietest 100 ms in the last CUT_SEARCH_SEC, so a
    cut rarely lands inside a word."""
    w, hop = RATE // 10, RATE // 20
    lo = max(0, len(audio) - int(CUT_SEARCH_SEC * RATE))
    tail = audio[lo:]
    starts = range(0, len(tail) - w + 1, hop)
    energy = [float(np.dot(tail[s:s + w], tail[s:s + w])) for s in starts]
    return lo + starts[int(np.argmin(energy))] + w // 2


class _Channel:
    def __init__(self, path: str, speaker: str) -> None:
        self.tail = _WavTail(path)
        self.speaker = speaker
        self.pending = _EMPTY
        self.base_sec = 0.0

    def next_chunk(self) -> tuple[float, np.ndarray] | None:
        """(start_sec, audio) of the next piece to transcribe, or None if not enough yet."""
        self.pending = np.concatenate([self.pending, self.tail.read_new()])
        if len(self.pending) < MIN_CHUNK_SEC * RATE:
            return None
        cut = _quiet_cut(self.pending)
        chunk, self.pending = self.pending[:cut], self.pending[cut:]
        start = self.base_sec
        self.base_sec += cut / RATE
        return start, chunk


class LiveTranscript:
    def __init__(self, cfg: Config, meeting_id: str, system_wav: str | None,
                 mic_wav: str | None, on_update: Callable[[], None]) -> None:
        self.meeting_id = meeting_id
        self.state = "loading"      # loading | running | failed
        self.error: str | None = None
        self._model_name = cfg.parakeet_model
        self._them = cfg.them_label
        self._channels = [_Channel(p, label) for p, label in
                          ((mic_wav, cfg.me_label), (system_wav, cfg.them_label)) if p]
        self._segments: list[Segment] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._on_update = on_update
        self._thread = threading.Thread(target=self._run, name="live-transcript", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            # MLX streams are per thread, so the model is loaded and used only here.
            import mlx.core as mx
            from parakeet_mlx import from_pretrained
            from parakeet_mlx.audio import get_logmel
            mx.set_cache_limit(0)
            model = from_pretrained(self._model_name)
            self.state = "running"
            self._on_update()
            while not self._stop.wait(POLL_SEC):
                added = False
                for ch in self._channels:
                    # Loop so a channel catches up after a daemon restart mid-recording.
                    while not self._stop.is_set() and (piece := ch.next_chunk()) is not None:
                        start, audio = piece
                        result = model.generate(get_logmel(mx.array(audio),
                                                           model.preprocessor_config))[0]
                        segs = [Segment(int((start + s.start) * 1000), int((start + s.end) * 1000),
                                        s.text.strip(), ch.speaker)
                                for s in result.sentences if s.text.strip()]
                        if segs:
                            with self._lock:
                                self._segments.extend(segs)
                            added = True
                if added:
                    self._on_update()
            mx.clear_cache()
        except Exception as e:
            self.state, self.error = "failed", str(e)
            self._on_update()

    def snapshot(self) -> dict:
        with self._lock:
            segs = sorted(self._segments, key=lambda s: s.start_ms)
        return {"meeting_id": self.meeting_id, "state": self.state, "error": self.error,
                "lines": _group(_drop_echoes(segs, self._them))}


def _drop_echoes(segs: list[Segment], them: str) -> list[Segment]:
    """Drop mic copies of what the system channel already heard (speakers
    leaking into the mic). The system copy is the cleaner one to keep."""
    sys_segs = [s for s in segs if s.speaker == them]
    sys_starts = [s.start_ms for s in sys_segs]
    sys_words = [_word_set(s.text) for s in sys_segs]
    kept = []
    for s in segs:
        if s.speaker != them:
            words = _word_set(s.text)
            lo = bisect.bisect_left(sys_starts, s.start_ms - ECHO_WINDOW_MS)
            hi = bisect.bisect_right(sys_starts, s.start_ms + ECHO_WINDOW_MS)
            if any(_containment(words, sys_words[j]) >= ECHO_SIMILARITY for j in range(lo, hi)):
                continue
        kept.append(s)
    return kept


def _group(segs: list[Segment]) -> list[dict]:
    """Join consecutive same-speaker sentences into lines, like _render_dialogue."""
    lines: list[dict] = []
    last_end = None
    for s in segs:
        cur = lines[-1] if lines else None
        if (cur and cur["speaker"] == s.speaker and s.start_ms - last_end <= GAP_BREAK_MS
                and s.end_ms - cur["start_ms"] <= MAX_LINE_MS):
            cur["text"] += " " + s.text
        else:
            lines.append({"start_ms": s.start_ms, "speaker": s.speaker, "text": s.text})
        last_end = s.end_ms
    return lines
