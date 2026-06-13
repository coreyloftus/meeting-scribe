"""scribe — record a meeting, transcribe it locally, summarise it, file it.

Subcommands:
  scribe start            Begin recording (system audio + mic)
  scribe stop             Stop recording and process it
  scribe process <path>   Process an existing recording or audio file
  scribe doctor           Check dependencies, permissions, and config
  scribe config [--init]  Show resolved config (or write a starter config.json)
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import config as config_mod
from . import recorder
from .config import Config, REPO_ROOT, EXAMPLE_CONFIG, DEFAULT_USER_CONFIG
from .process import process
from .recorder import HELPER_BIN


def _load(args) -> Config:
    return config_mod.load(getattr(args, "config", None))


# --- start / stop -----------------------------------------------------------

def cmd_start(args) -> int:
    cfg = _load(args)
    try:
        s = recorder.start(cfg)
    except recorder.RecorderError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print("● Recording started.")
    if s.system_wav:
        print(f"  system: {s.system_wav}")
    if s.mic_wav:
        print(f"  mic:    {s.mic_wav}")
    print("Run `scribe stop` when the meeting ends.")
    return 0


def cmd_stop(args) -> int:
    cfg = _load(args)
    try:
        s = recorder.stop(cfg)
    except recorder.RecorderError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print("■ Recording stopped.")
    try:
        for line in process(cfg, s.system_wav, s.mic_wav, audio_label=s.base + ".*.wav"):
            print(f"  {line}")
    except Exception as e:
        print(f"Processing failed: {e}", file=sys.stderr)
        print(f"Your audio is safe. Retry with:  scribe process {s.base}", file=sys.stderr)
        return 1
    return 0


# --- process existing -------------------------------------------------------

def _resolve_pair(path: Path) -> tuple[str | None, str | None, str]:
    """Given a base path or a single file, find the system/mic wav pair."""
    s = str(path)
    # A recording base like ".../2026-06-13_10-00-00" (with .system.wav/.mic.wav siblings)
    base = s
    for suffix in (".system.wav", ".mic.wav", ".wav"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    sys_wav = base + ".system.wav"
    mic_wav = base + ".mic.wav"
    if Path(sys_wav).is_file() or Path(mic_wav).is_file():
        return (sys_wav if Path(sys_wav).is_file() else None,
                mic_wav if Path(mic_wav).is_file() else None, base + ".*.wav")
    # Otherwise treat the given file as a single mixed source.
    if path.is_file():
        return (str(path), None, str(path))
    return (None, None, s)


def cmd_process(args) -> int:
    cfg = _load(args)
    sys_wav, mic_wav, label = _resolve_pair(Path(args.path).expanduser())
    if not sys_wav and not mic_wav:
        print(f"No audio found at: {args.path}", file=sys.stderr)
        return 1
    try:
        for line in process(cfg, sys_wav, mic_wav, audio_label=label):
            print(f"  {line}")
    except Exception as e:
        print(f"Processing failed: {e}", file=sys.stderr)
        return 1
    return 0


# --- doctor -----------------------------------------------------------------

def _ok(b: bool) -> str:
    return "✓" if b else "✗"


def cmd_doctor(args) -> int:
    cfg = _load(args)
    print("meeting-scribe doctor\n")

    tools = {t: shutil.which(t) for t in ("ffmpeg", "ffprobe", cfg.whisper_cli, "SwitchAudioSource")}
    for t, path in tools.items():
        print(f"  {_ok(bool(path))} {t}: {path or 'NOT FOUND'}")

    helper = HELPER_BIN.exists()
    print(f"  {_ok(helper)} system-audio helper: "
          f"{HELPER_BIN if helper else 'NOT BUILT — run bash scripts/build_helper.sh'}")

    model = cfg.whisper_model
    model_ok = bool(model and Path(model).is_file())
    print(f"  {_ok(model_ok)} whisper model: {model or '(unset)'}")

    print(f"  {_ok(bool(cfg.source))} config file: {cfg.source or '(using defaults only)'}")
    if cfg.llm_backend == "claude_cli":
        cli = shutil.which(cfg.claude_cli)
        print(f"  {_ok(bool(cli))} LLM backend: claude_cli (uses your Claude Code login/subscription)")
        print(f"    {_ok(bool(cli))} `{cfg.claude_cli}`: {cli or 'NOT FOUND on PATH'}")
    else:
        print(f"  {_ok(bool(cfg.anthropic_key))} LLM backend: api — Anthropic API key "
              f"{'set' if cfg.anthropic_key else 'MISSING (set ANTHROPIC_API_KEY or anthropic.api_key)'}")
    print(f"    model: {cfg.model}")

    outs = cfg.enabled_outputs()
    print(f"  outputs enabled: {', '.join(outs) or '(none)'}")
    if "notion" in outs:
        tok = bool(cfg.notion_token)
        db = bool(cfg.get('outputs', 'notion', 'database_id'))
        print(f"    {_ok(tok)} notion token   {_ok(db)} notion database_id")

    print("\nNote: the terminal needs Screen Recording permission for system-audio capture\n"
          "(System Settings > Privacy & Security > Screen Recording).")
    return 0


# --- config -----------------------------------------------------------------

def cmd_config(args) -> int:
    if args.init:
        dest = Path(args.path).expanduser() if args.path else DEFAULT_USER_CONFIG
        if dest.exists() and not args.force:
            print(f"{dest} already exists. Use --force to overwrite.", file=sys.stderr)
            return 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(EXAMPLE_CONFIG.read_text())
        print(f"Wrote starter config to {dest}\nEdit it to add your API key(s).")
        return 0

    cfg = _load(args)
    print(f"config source: {cfg.source or '(defaults only)'}")
    import json
    print(json.dumps(cfg.data, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scribe", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="Path to config.json (overrides auto-discovery)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="Begin recording").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="Stop recording and process").set_defaults(func=cmd_stop)

    pp = sub.add_parser("process", help="Process an existing recording/audio file")
    pp.add_argument("path", help="Recording base path or an audio file")
    pp.set_defaults(func=cmd_process)

    sub.add_parser("doctor", help="Check dependencies and config").set_defaults(func=cmd_doctor)

    pc = sub.add_parser("config", help="Show or initialise config")
    pc.add_argument("--init", action="store_true", help="Write a starter config.json")
    pc.add_argument("--force", action="store_true", help="Overwrite an existing config on --init")
    pc.add_argument("path", nargs="?", help="Where to write config on --init")
    pc.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
