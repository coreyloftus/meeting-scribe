"""Pluggable output destinations.

Each writer takes the same `Note` and sends it somewhere. Which writers run
automatically is controlled by config (`outputs.<key>.enabled`); any registered
writer can also be targeted individually via `write_one` (the daemon's "push"
action). See base.py for the registry contract.
"""
from __future__ import annotations

from dataclasses import dataclass

from .base import OutputResult, REGISTRY, write_all, write_one  # noqa: F401


@dataclass
class Note:
    title: str
    date: str           # YYYY-MM-DD
    slug: str
    summary_md: str
    transcript: str
    audio_path: str | None = None
    user_notes: str | None = None   # notes typed by the user during the meeting

    def full_markdown(self) -> str:
        parts = [
            f"# {self.title}",
            "",
            f"_Date: {self.date}_",
        ]
        if self.audio_path:
            parts.append(f"_Audio: {self.audio_path}_")
        parts += ["", self.summary_md]
        if self.user_notes and self.user_notes.strip():
            parts += ["", "---", "", "## My Notes", "", self.user_notes.strip()]
        parts += ["", "---", "", "## Full Transcript", "", self.transcript]
        return "\n".join(parts)
