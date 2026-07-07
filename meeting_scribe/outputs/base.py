"""Output plugin registry.

Every destination ("markdown", "notion", "gdocs", "gdrive", …) is one module
that exposes:

    KEY: str                    # registry key, e.g. "gdrive"
    LABEL: str                  # human label, e.g. "Google Drive"
    def is_configured(cfg) -> bool
    def write(cfg, note, options: dict | None = None) -> OutputResult

Modules are imported lazily so a destination's dependencies (requests, google
libs, …) are only needed when that destination is actually used. Adding a new
destination = one new module + one line in _SPECS.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field


@dataclass
class OutputResult:
    target: str                 # registry key
    ok: bool
    url: str | None = None      # remote URL or local file path
    detail: str = ""            # error text on failure, extra info on success

    def line(self) -> str:
        """Human-readable one-liner for the CLI."""
        if self.ok:
            return f"{self.target}: {self.url or self.detail or 'ok'}"
        return f"{self.target}: FAILED — {self.detail}"


@dataclass
class OutputSpec:
    key: str
    label: str
    module: str
    _mod: object = field(default=None, repr=False)

    def load(self):
        if self._mod is None:
            self._mod = importlib.import_module(self.module)
        return self._mod

    def is_configured(self, cfg) -> bool:
        try:
            return bool(self.load().is_configured(cfg))
        except Exception:
            return False

    def write(self, cfg, note, options: dict | None = None) -> OutputResult:
        try:
            return self.load().write(cfg, note, options)
        except Exception as e:  # one output failing shouldn't lose the others
            return OutputResult(target=self.key, ok=False, detail=str(e))


_SPECS: list[OutputSpec] = [
    OutputSpec("markdown", "Markdown file", "meeting_scribe.outputs.markdown"),
    OutputSpec("notion", "Notion", "meeting_scribe.outputs.notion"),
    OutputSpec("gdrive", "Google Drive", "meeting_scribe.outputs.gdrive"),
    OutputSpec("gdocs", "Google Docs", "meeting_scribe.outputs.gdocs"),
]

REGISTRY: dict[str, OutputSpec] = {s.key: s for s in _SPECS}


def write_all(cfg, note) -> list[OutputResult]:
    """Run every output enabled in config. Never raises for a single failure."""
    return [REGISTRY[key].write(cfg, note)
            for key in cfg.enabled_outputs() if key in REGISTRY]


def write_one(cfg, note, target: str, options: dict | None = None) -> OutputResult:
    """Push to a single destination regardless of its enabled flag."""
    spec = REGISTRY.get(target)
    if spec is None:
        return OutputResult(target=target, ok=False,
                            detail=f"unknown output target {target!r} "
                                   f"(known: {', '.join(REGISTRY)})")
    return spec.write(cfg, note, options)
