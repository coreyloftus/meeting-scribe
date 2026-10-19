"""Write the note as a markdown file (e.g. into an Obsidian vault)."""
from __future__ import annotations

from pathlib import Path

from ..config import Config, expand
from .base import OutputResult

KEY = "markdown"
LABEL = "Markdown file"


def is_configured(cfg: Config) -> bool:
    return True  # only needs a writable directory, which we create


def write(cfg: Config, note, options: dict | None = None) -> OutputResult:
    out_dir = expand(cfg.get("outputs", "markdown", "dir", default="~/Documents/meeting-transcripts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    previous = Path(note.previous_urls.get(KEY, ""))
    path = out_dir / f"{note.date}-{note.slug}.md"
    # Another meeting on the same day with the same slug must not overwrite this one.
    n = 2
    while path.exists() and path != previous:
        path = out_dir / f"{note.date}-{note.slug}-{n}.md"
        n += 1
    path.write_text(note.full_markdown())
    # A re-push that got a new slug: remove this meeting's old file.
    if previous.parent == out_dir and previous.suffix == ".md" and previous != path:
        previous.unlink(missing_ok=True)
    return OutputResult(target=KEY, ok=True, url=str(path))
