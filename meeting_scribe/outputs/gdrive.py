"""Upload the user's selected meeting files to Google Drive."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from ..config import Config
from ..integrations import google_auth
from .base import OutputResult

KEY = "gdrive"
LABEL = "Google Drive"
_FORMATS = {"md": ("md", "text/markdown"), "txt": ("txt", "text/plain")}


def is_configured(cfg: Config) -> bool:
    return google_auth.is_connected(cfg)


def write(cfg: Config, note, options: dict | None = None) -> OutputResult:
    options = options or {}
    def setting(key, default):
        value = options.get(key)
        return cfg.get("outputs", "gdrive", key, default=default) if value is None else value
    ext, mime = _FORMATS.get(setting("format", "md"), _FORMATS["md"])
    folder = setting("folder_id", "") or None
    summary, transcript, recording = (setting("summary", True), setting("transcript", True),
                                      setting("recording", False))
    if not any((summary, transcript, recording)):
        return OutputResult(target=KEY, ok=False, detail="Choose at least one Drive upload type in Settings.")
    urls, failures = [], []
    prefix = f"{note.date}-{note.slug}"
    documents = []
    if summary:
        documents.append((f"{prefix}-summary.{ext}", note.full_markdown(include_transcript=False)))
    if transcript:
        documents.append((f"{prefix}-transcript.txt", note.transcript))
    for name, content in documents:
        try:
            _, url = google_auth.upload_file(cfg, name, content,
                                             "text/plain" if name.endswith('.txt') else mime,
                                             folder_id=folder)
            urls.append(url)
        except Exception as e:
            failures.append(f"{name}: {e}")
    if recording:
        if not note.audio_files:
            failures.append("No recording files available.")
        for source in note.audio_files:
            path = Path(source)
            try:
                if not path.is_file():
                    raise ValueError("Recording file is missing; restore it before retrying.")
                channel = path.stem.rsplit('.', 1)[-1]
                name = f"{prefix}-{channel}.flac"
                if path.suffix.lower() == '.flac':
                    urls.append(google_auth.upload_recording(cfg, path, name, folder))
                else:
                    with tempfile.TemporaryDirectory(prefix="scribe-drive-", dir=path.parent) as tmp:
                        flac = Path(tmp) / path.with_suffix('.flac').name
                        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(path),
                                        '-c:a', 'flac', str(flac)], check=True, capture_output=True)
                        urls.append(google_auth.upload_recording(cfg, flac, name, folder))
            except subprocess.CalledProcessError as e:
                failures.append(f"{path.name}: FLAC conversion failed: {e.stderr.decode(errors='replace')[-500:]}")
            except Exception as e:
                failures.append(f"{path.name}: {e}")
    url = f"https://drive.google.com/drive/folders/{folder}" if folder and urls else (urls[0] if urls else None)
    detail = f"Uploaded {len(urls)} file(s)." + (" Failed: " + "; ".join(failures) if failures else "")
    return OutputResult(target=KEY, ok=not failures, url=url, detail=detail)
