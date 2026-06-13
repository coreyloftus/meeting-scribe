"""Turn recorded audio into a transcript, a summary, and output notes."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .config import Config
from . import audio, transcribe, llm
from .outputs import Note, write_all


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


def process(cfg: Config, system_wav: str | None, mic_wav: str | None,
            audio_label: str | None = None) -> list[str]:
    """Full pipeline. Returns human-readable result lines for the CLI to print."""
    out: list[str] = []

    # Warn (don't fail) if the mic was captured in low-quality Bluetooth mode.
    if mic_wav and Path(mic_wav).is_file():
        bad = audio.looks_like_lowquality_bluetooth(Path(mic_wav))
        if bad:
            out.append(
                f"⚠  Mic was captured at {bad} Hz — your input is a Bluetooth headset in "
                f"hands-free mode (low quality). For full quality, set the input to the built-in "
                f"mic and keep AirPods as output only.")

    # Per-channel silence checks.
    for label, wav in (("mic", mic_wav), ("system", system_wav)):
        if wav and Path(wav).is_file():
            db = audio.mean_volume_db(Path(wav))
            if audio.is_silent(db):
                out.append(f"⚠  {label} channel looks silent ({db} dB).")

    out.append("Transcribing…")
    transcript = transcribe.transcribe(cfg, system_wav, mic_wav)
    if not transcript.strip():
        raise RuntimeError("Transcript was empty — check the recordings and whisper model.")

    out.append("Summarizing with Claude…")
    slug = llm.make_slug(cfg, transcript)
    summary = llm.summarize(cfg, transcript)

    today = _today()
    note = Note(
        title=f"{today} {slug.replace('-', ' ')}",
        date=today,
        slug=slug,
        summary_md=summary,
        transcript=transcript,
        audio_path=audio_label,
    )

    out += write_all(cfg, note)
    return out
