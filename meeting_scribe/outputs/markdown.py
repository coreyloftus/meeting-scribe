"""Write the note as a markdown file (e.g. into an Obsidian vault)."""
from __future__ import annotations

from pathlib import Path

from ..config import Config, expand
from . import Note


def write(cfg: Config, note: Note) -> str:
    out_dir = expand(cfg.get("outputs", "markdown", "dir", default="~/Documents/meeting-transcripts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{note.date}-{note.slug}.md"
    path.write_text(note.full_markdown())
    return f"markdown: {path}"
