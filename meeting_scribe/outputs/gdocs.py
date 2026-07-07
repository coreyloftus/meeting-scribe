"""Create a native Google Doc from the note.

Uses Drive's built-in markdown import (upload text/markdown with a Google Doc
target mimeType) — Drive renders headings, bullets, and checklists natively,
which replaces a few hundred lines of Docs-API batchUpdate bookkeeping.
"""
from __future__ import annotations

from ..config import Config
from ..integrations import google_auth
from .base import OutputResult

KEY = "gdocs"
LABEL = "Google Docs"


def is_configured(cfg: Config) -> bool:
    return google_auth.is_connected(cfg)


def write(cfg: Config, note, options: dict | None = None) -> OutputResult:
    options = options or {}
    folder = options.get("folder_id") or cfg.get("outputs", "gdocs", "folder_id", default="") or None

    _, url = google_auth.upload_file(
        cfg, name=note.title,
        content=note.full_markdown(), source_mime="text/markdown",
        target_mime=google_auth.DOC_MIME, folder_id=folder)
    return OutputResult(target=KEY, ok=True, url=url)
