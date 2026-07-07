"""Load and validate meeting-scribe configuration from config.json.

Resolution order for the config file:
  1. $MEETING_SCRIBE_CONFIG (if set)
  2. ./config.json (next to the repo)
  3. ~/.config/meeting-scribe/config.json

Secrets may live in the file or in the environment; env always wins:
  ANTHROPIC_API_KEY  ->  anthropic.api_key
  NOTION_TOKEN       ->  outputs.notion.token
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_USER_CONFIG = Path.home() / ".config" / "meeting-scribe" / "config.json"
EXAMPLE_CONFIG = REPO_ROOT / "config.example.json"


class ConfigError(Exception):
    pass


def expand(path: str | None) -> Path | None:
    if not path:
        return None
    return Path(os.path.expanduser(os.path.expandvars(path)))


def find_config_path() -> Path | None:
    env = os.environ.get("MEETING_SCRIBE_CONFIG")
    if env:
        return Path(env).expanduser()
    candidates = [REPO_ROOT / "config.json", DEFAULT_USER_CONFIG]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Thin typed-ish accessor over the merged config dict."""

    def __init__(self, data: dict[str, Any], source: Path | None):
        self.data = data
        self.source = source

    # --- generic access -----------------------------------------------------
    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    # --- anthropic ----------------------------------------------------------
    @property
    def llm_backend(self) -> str:
        """'api' (Anthropic API key, pay-as-you-go) or 'claude_cli' (local
        `claude` CLI / Claude Code — uses whatever it's logged in with, e.g. a
        Pro/Max subscription)."""
        return self.get("anthropic", "backend", default="api")

    @property
    def claude_cli(self) -> str:
        return self.get("anthropic", "claude_cli", default="claude")

    @property
    def anthropic_key(self) -> str:
        return os.environ.get("ANTHROPIC_API_KEY") or self.get("anthropic", "api_key", default="") or ""

    @property
    def model(self) -> str:
        return self.get("anthropic", "model", default="claude-haiku-4-5-20251001")

    @property
    def max_tokens(self) -> int:
        return int(self.get("anthropic", "max_tokens", default=4096))

    # --- recording ----------------------------------------------------------
    @property
    def recordings_dir(self) -> Path:
        return expand(self.get("recording", "dir", default="~/Recordings/meetings"))

    @property
    def capture_system_audio(self) -> bool:
        return bool(self.get("recording", "capture_system_audio", default=True))

    @property
    def capture_mic(self) -> bool:
        return bool(self.get("recording", "capture_mic", default=True))

    @property
    def mic_device(self) -> str | None:
        return self.get("recording", "mic_device", default=None)

    # --- transcription ------------------------------------------------------
    @property
    def whisper_cli(self) -> str:
        return self.get("transcription", "whisper_cli", default="whisper-cli")

    @property
    def whisper_model(self) -> Path | None:
        return expand(self.get("transcription", "whisper_model", default=None))

    @property
    def vad_model(self) -> Path | None:
        """Silero VAD model for whisper.cpp (--vad). Optional; skipped if the
        file doesn't exist."""
        return expand(self.get("transcription", "vad_model", default=None))

    @property
    def whisper_threads(self) -> int:
        return int(self.get("transcription", "threads", default=8))

    @property
    def language(self) -> str:
        return self.get("transcription", "language", default="auto")

    @property
    def speaker_labels(self) -> bool:
        return bool(self.get("transcription", "speaker_labels", default=True))

    @property
    def me_label(self) -> str:
        return self.get("transcription", "me_label", default="Me")

    @property
    def them_label(self) -> str:
        return self.get("transcription", "them_label", default="Them")

    # --- prompts ------------------------------------------------------------
    @property
    def slug_prompt(self) -> str:
        return self.get("prompts", "slug", default="").strip()

    @property
    def summary_prompt(self) -> str:
        return self.get("prompts", "summary", default="").strip()

    # --- outputs ------------------------------------------------------------
    @property
    def notion_token(self) -> str:
        return os.environ.get("NOTION_TOKEN") or self.get("outputs", "notion", "token", default="") or ""

    OUTPUT_KEYS = ("markdown", "notion", "gdocs", "gdrive")

    def enabled_outputs(self) -> list[str]:
        return [k for k in self.OUTPUT_KEYS
                if self.get("outputs", k, "enabled", default=(k == "markdown"))]

    # --- google -------------------------------------------------------------
    @property
    def google_client_id(self) -> str:
        return os.environ.get("GOOGLE_CLIENT_ID") or self.get("google", "client_id", default="") or ""

    @property
    def google_client_secret(self) -> str:
        return os.environ.get("GOOGLE_CLIENT_SECRET") or self.get("google", "client_secret", default="") or ""

    @property
    def google_token_path(self) -> Path:
        return expand(self.get("google", "token_path",
                               default="~/.config/meeting-scribe/google_token.json"))


def load(path: str | Path | None = None) -> Config:
    if path is not None:
        cfg_path = Path(path).expanduser()
    else:
        cfg_path = find_config_path()

    # Start from the example so missing keys always have sane defaults.
    base: dict[str, Any] = {}
    if EXAMPLE_CONFIG.is_file():
        base = json.loads(EXAMPLE_CONFIG.read_text())

    if cfg_path is None or not cfg_path.is_file():
        return Config(base, None)

    try:
        user = json.loads(cfg_path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"{cfg_path} is not valid JSON: {e}") from e

    return Config(_deep_merge(base, user), cfg_path)
