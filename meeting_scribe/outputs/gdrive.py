"""Upload the note to Google Drive as a plain .md or .txt file."""
from __future__ import annotations

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
    fmt = options.get("format") or cfg.get("outputs", "gdrive", "format", default="md")
    ext, mime = _FORMATS.get(fmt, _FORMATS["md"])
    folder = options.get("folder_id") or cfg.get("outputs", "gdrive", "folder_id", default="") or None

    _, url = google_auth.upload_file(
        cfg, name=f"{note.date}-{note.slug}.{ext}",
        content=note.full_markdown(), source_mime=mime, folder_id=folder)
    return OutputResult(target=KEY, ok=True, url=url)
