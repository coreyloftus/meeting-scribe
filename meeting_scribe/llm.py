"""Anthropic calls: a topic slug and an action-items summary from a transcript.

Uses the official `anthropic` SDK. The model and prompts come from config so a
user can swap models (e.g. claude-haiku-4-5 for cost, claude-sonnet-4-6 or
claude-opus-4-8 for quality) and customise the output format without touching
code.
"""
from __future__ import annotations

import re
import shutil
import subprocess

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


def _generate(cfg: Config, prompt: str, max_tokens: int) -> str:
    """Dispatch to the configured backend: the Anthropic API, or the local
    `claude` CLI (Claude Code) which runs against whatever it's logged in with."""
    if cfg.llm_backend == "claude_cli":
        return _generate_cli(cfg, prompt)
    msg = _client(cfg).messages.create(
        model=cfg.model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return _text(msg)


def _generate_cli(cfg: Config, prompt: str) -> str:
    exe = shutil.which(cfg.claude_cli) or cfg.claude_cli
    cmd = [exe, "-p"]
    if cfg.model:
        cmd += ["--model", cfg.model]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as e:
        raise LLMError(
            f"`{cfg.claude_cli}` not found. Install Claude Code, or set anthropic.backend "
            f"to 'api'.") from e
    except subprocess.TimeoutExpired as e:
        raise LLMError("claude CLI timed out after 600s.") from e
    if r.returncode != 0:
        raise LLMError(f"claude CLI failed: {(r.stderr or r.stdout)[:300]}")
    return r.stdout.strip()


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "meeting-notes"


def make_slug(cfg: Config, transcript: str) -> str:
    """Short kebab-case topic slug for filenames/titles."""
    prompt = cfg.slug_prompt or (
        "Generate a short 2-4 word lowercase hyphenated slug for this meeting.")
    out = _generate(cfg, f"{prompt}\n\nTranscript (first 2000 chars):\n{transcript[:2000]}", max_tokens=64)
    return slugify(out)


def summarize(cfg: Config, transcript: str) -> str:
    """Markdown summary with action items, decisions, and takeaways."""
    prompt = cfg.summary_prompt or (
        "Summarize this meeting transcript into Action Items, Key Decisions, and Key Takeaways in markdown.")
    return _generate(cfg, f"{prompt}\n\nTranscript:\n\n{transcript}", max_tokens=cfg.max_tokens)
