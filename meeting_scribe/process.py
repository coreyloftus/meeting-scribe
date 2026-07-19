"""Turn recorded audio into a transcript, a summary, and output notes.

The pipeline is split into observable stages so the daemon can run it as a
background job with progress events, and so a note can be rebuilt later (from
the persisted transcript) to push to a single new destination:

    check_audio()      warnings about silence / bluetooth capture
    make_transcript()  whisper both channels, persist <base>.transcript.txt
    build_note()       slug + summary via the LLM -> Note
    write_all()        every enabled output (outputs/base.py registry)

`process()` composes them all and keeps the CLI's one-call behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from .config import Config
from . import audio, transcribe, llm
from .outputs import Note, OutputResult, write_all

# on_phase(phase, pct) — pct is 0..1 or None when indeterminate.
ProgressFn = Callable[[str, float | None], None]


@dataclass
class ProcessResult:
    note: Note
    transcript_path: Path | None
    outputs: list[OutputResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        """Human-readable result lines for the CLI to print."""
        out = [f"⚠  {w}" for w in self.warnings]
        if self.transcript_path:
            out.append(f"transcript: {self.transcript_path}")
        out += [r.line() for r in self.outputs]
        return out


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def _noop(phase: str, pct: float | None) -> None:
    pass


def derive_base(system_wav: str | None, mic_wav: str | None) -> Path | None:
    """`…/2026-07-06_10-00-00` from either channel's wav path."""
    for wav, suffix in ((system_wav, ".system.wav"), (mic_wav, ".mic.wav")):
        if wav:
            s = str(wav)
            return Path(s[:-len(suffix)] if s.endswith(suffix) else s.rsplit(".", 1)[0])
    return None


def transcript_path_for(base: Path) -> Path:
    return Path(str(base) + ".transcript.txt")


def notes_path_for(base: Path) -> Path:
    return Path(str(base) + ".notes.md")


def check_audio(system_wav: str | None, mic_wav: str | None) -> list[str]:
    """Non-fatal capture-quality warnings."""
    warnings: list[str] = []
    if mic_wav and Path(mic_wav).is_file():
        bad = audio.looks_like_lowquality_bluetooth(Path(mic_wav))
        if bad:
            warnings.append(
                f"Mic was captured at {bad} Hz — your input is a Bluetooth headset in "
                f"hands-free mode (low quality). For full quality, set the input to the built-in "
                f"mic and keep AirPods as output only.")
    for label, wav in (("mic", mic_wav), ("system", system_wav)):
        if wav and Path(wav).is_file():
            db = audio.mean_volume_db(Path(wav))
            if audio.is_silent(db):
                warnings.append(f"{label} channel looks silent ({db} dB).")
    return warnings


def make_transcript(cfg: Config, system_wav: str | None, mic_wav: str | None) -> tuple[str, Path | None]:
    """Transcribe and persist the transcript next to the audio. Returns (text, path)."""
    transcript = transcribe.transcribe(cfg, system_wav, mic_wav)
    if not transcript.strip():
        raise RuntimeError("Transcript was empty — check the recordings and whisper model.")
    path = None
    base = derive_base(system_wav, mic_wav)
    if base is not None:
        path = transcript_path_for(base)
        try:
            path.write_text(transcript)
        except OSError:
            path = None  # transcript still returned; persistence is best-effort
    return transcript, path


def build_note(cfg: Config, transcript: str, audio_label: str | None = None,
               user_notes: str | None = None, meeting_date: str | None = None) -> Note:
    """Slug + summary via the LLM, assembled into a Note."""
    slug = llm.make_slug(cfg, transcript)
    summary = llm.summarize(cfg, transcript, user_notes=user_notes)
    day = meeting_date or _today()
    return Note(
        title=f"{day} {slug.replace('-', ' ')}",
        date=day,
        slug=slug,
        summary_md=summary,
        transcript=transcript,
        audio_path=audio_label,
        user_notes=user_notes,
    )


def process(cfg: Config, system_wav: str | None, mic_wav: str | None,
            audio_label: str | None = None, user_notes: str | None = None,
            meeting_date: str | None = None,
            on_phase: ProgressFn | None = None) -> ProcessResult:
    """Full pipeline: transcribe -> summarize -> write enabled outputs."""
    on_phase = on_phase or _noop

    # If the user took notes during the meeting, pick them up from disk unless
    # the caller passed them explicitly.
    base = derive_base(system_wav, mic_wav)
    if user_notes is None and base is not None:
        np = notes_path_for(base)
        if np.is_file():
            user_notes = np.read_text()

    warnings = check_audio(system_wav, mic_wav)

    # Reuse a transcript newer than its audio (a retry after a failed
    # summarize step) rather than re-running whisper on the whole meeting.
    transcript, transcript_path = "", None
    if base is not None:
        tp = transcript_path_for(base)
        if tp.is_file():
            wav_mtimes = [Path(w).stat().st_mtime for w in (system_wav, mic_wav)
                          if w and Path(w).is_file()]
            if wav_mtimes and tp.stat().st_mtime >= max(wav_mtimes):
                transcript, transcript_path = tp.read_text(), tp
    if not transcript.strip():
        on_phase("transcribing", None)
        transcript, transcript_path = make_transcript(cfg, system_wav, mic_wav)

    on_phase("summarizing", None)
    note = build_note(cfg, transcript, audio_label=audio_label,
                      user_notes=user_notes, meeting_date=meeting_date)

    on_phase("writing_outputs", None)
    results = write_all(cfg, note)

    return ProcessResult(note=note, transcript_path=transcript_path,
                         outputs=results, warnings=warnings)
