"""Anthropic calls: a topic slug and an action-items summary from a transcript.

Uses the official `anthropic` SDK. The model and prompts come from config so a
user can swap models (e.g. claude-haiku-4-5 for cost, claude-sonnet-4-6 or
claude-opus-4-8 for quality) and customise the output format without touching
code.
"""
from __future__ import annotations

import re

from .config import Config


class LLMError(Exception):
    pass


def _client(cfg: Config):
    key = cfg.anthropic_key
    if not key:
        raise LLMError(
            "No Anthropic API key. Set ANTHROPIC_API_KEY or anthropic.api_key in config.json.")
    try:
        import anthropic
    except ImportError as e:
        raise LLMError("The `anthropic` package isn't installed. Run: pip install -e .") from e
    return anthropic.Anthropic(api_key=key)


def _text(message) -> str:
    return "".join(b.text for b in message.content if b.type == "text").strip()


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "meeting-notes"


def make_slug(cfg: Config, transcript: str) -> str:
    """Short kebab-case topic slug for filenames/titles."""
    client = _client(cfg)
    prompt = cfg.slug_prompt or (
        "Generate a short 2-4 word lowercase hyphenated slug for this meeting.")
    msg = client.messages.create(
        model=cfg.model,
        max_tokens=64,
        messages=[{"role": "user", "content": f"{prompt}\n\nTranscript (first 2000 chars):\n{transcript[:2000]}"}],
    )
    return slugify(_text(msg))


def summarize(cfg: Config, transcript: str) -> str:
    """Markdown summary with action items, decisions, and takeaways."""
    client = _client(cfg)
    prompt = cfg.summary_prompt or (
        "Summarize this meeting transcript into Action Items, Key Decisions, and Key Takeaways in markdown.")
    msg = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        messages=[{"role": "user", "content": f"{prompt}\n\nTranscript:\n\n{transcript}"}],
    )
    return _text(msg)
