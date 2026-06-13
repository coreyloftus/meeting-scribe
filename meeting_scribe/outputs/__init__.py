"""Pluggable output destinations.

Each writer takes the same `Note` and sends it somewhere. Which writers run is
controlled entirely by config (`outputs.markdown.enabled`, `outputs.notion.enabled`),
so a user who doesn't use Notion just leaves it disabled.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Config


@dataclass
class Note:
    title: str
    date: str           # YYYY-MM-DD
    slug: str
    summary_md: str
    transcript: str
    audio_path: str | None = None

    def full_markdown(self) -> str:
        parts = [
            f"# {self.title}",
            "",
            f"_Date: {self.date}_",
        ]
        if self.audio_path:
            parts.append(f"_Audio: {self.audio_path}_")
        parts += ["", self.summary_md, "", "---", "", "## Full Transcript", "", self.transcript]
        return "\n".join(parts)


def write_all(cfg: Config, note: Note) -> list[str]:
    """Run every enabled output. Returns human-readable result lines."""
    from . import markdown as markdown_out
    from . import notion as notion_out

    results: list[str] = []
    for name in cfg.enabled_outputs():
        try:
            if name == "markdown":
                results.append(markdown_out.write(cfg, note))
            elif name == "notion":
                results.append(notion_out.write(cfg, note))
        except Exception as e:  # one output failing shouldn't lose the others
            results.append(f"{name}: FAILED — {e}")
    return results
