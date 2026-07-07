"""Write the note as a markdown file (e.g. into an Obsidian vault)."""
from __future__ import annotations

from ..config import Config, expand
from .base import OutputResult

KEY = "markdown"
LABEL = "Markdown file"


def is_configured(cfg: Config) -> bool:
    return True  # only needs a writable directory, which we create


def write(cfg: Config, note, options: dict | None = None) -> OutputResult:
    out_dir = expand(cfg.get("outputs", "markdown", "dir", default="~/Documents/meeting-transcripts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{note.date}-{note.slug}.md"
    path.write_text(note.full_markdown())
    return OutputResult(target=KEY, ok=True, url=str(path))
