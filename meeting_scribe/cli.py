"""scribe — record a meeting, transcribe it locally, summarise it, file it.

Subcommands:
  scribe start            Begin recording (system audio + mic)
  scribe stop             Stop recording and process it
  scribe process <path>   Process an existing recording or audio file
  scribe list             List captured meetings
  scribe daemon <cmd>     Manage the scribed background daemon
  scribe doctor           Check dependencies, permissions, and config
  scribe config [--init]  Show resolved config (or write a starter config.json)

start/stop/list prefer the scribed daemon (background processing, meetings
index, the app UI sees the same state); with `--local` — or when the daemon
isn't running — they fall back to the original in-process behavior.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import config as config_mod
from . import recorder
from .client import DaemonClient, DaemonError
from .config import Config, EXAMPLE_CONFIG, DEFAULT_USER_CONFIG
from .daemon.db import STAMP_RE
from .process import process
from .recorder import HELPER_BIN


def _load(args) -> Config:
    return config_mod.load(getattr(args, "config", None))


def _daemon(args) -> DaemonClient | None:
    """The daemon client, or None when --local was passed or it isn't up."""
    if getattr(args, "local", False):
        return None
    c = DaemonClient()
    return c if c.is_up() else None


# --- start / stop -----------------------------------------------------------

def cmd_start(args) -> int:
    d = _daemon(args)
    if d is not None:
        try:
            m = d.start()["meeting"]
        except DaemonError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"● Recording started (via daemon): {m['id']}")
        print("Run `scribe stop` when the meeting ends.")
        return 0

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
    d = _daemon(args)
    if d is not None:
        try:
            r = d.stop()
        except DaemonError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"■ Recording stopped: {r['meeting_id']}")
        print(f"  Processing in the background (job {r['job_id']}). "
              f"Watch with `scribe list`.")
        return 0

    cfg = _load(args)
    try:
        s = recorder.stop(cfg)
    except recorder.RecorderError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print("■ Recording stopped.")
    try:
        result = process(cfg, s.system_wav, s.mic_wav, audio_label=s.base + ".*.wav")
        for line in result.lines():
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
    # A bare meeting id (recording stamp) goes to the daemon as a reprocess job.
    d = _daemon(args)
    if d is not None and STAMP_RE.match(args.path):
        try:
            r = d.reprocess(args.path)
        except DaemonError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"Reprocessing {args.path} in the background (job {r['job_id']}).")
        return 0

    cfg = _load(args)
    sys_wav, mic_wav, label = _resolve_pair(Path(args.path).expanduser())
    if not sys_wav and not mic_wav:
        print(f"No audio found at: {args.path}", file=sys.stderr)
        return 1
    try:
        result = process(cfg, sys_wav, mic_wav, audio_label=label)
        for line in result.lines():
            print(f"  {line}")
    except Exception as e:
        print(f"Processing failed: {e}", file=sys.stderr)
        return 1
    return 0


# --- list --------------------------------------------------------------------

_STATUS_ICON = {"recording": "●", "queued": "…", "transcribing": "…",
                "summarizing": "…", "writing_outputs": "…", "done": "✓",
                "failed": "✗", "recorded": "·"}


def cmd_list(args) -> int:
    d = _daemon(args)
    if d is None:
        print("Daemon not running — showing raw recordings on disk.\n")
        cfg = _load(args)
        rec = cfg.recordings_dir
        stamps = sorted({f.name.split(".")[0] for f in rec.glob("*.wav")}, reverse=True) \
            if rec and rec.is_dir() else []
        for s in stamps[:args.limit]:
            print(f"  · {s}")
        if not stamps:
            print("  (no recordings found)")
        return 0

    try:
        meetings = d.meetings(limit=args.limit, q=args.query)
    except DaemonError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not meetings:
        print("No meetings yet.")
        return 0
    for m in meetings:
        icon = _STATUS_ICON.get(m["status"], "?")
        title = m.get("title") or m["id"]
        dur = m.get("duration_sec")
        dur_s = f"  {dur // 60}m{dur % 60:02d}s" if dur else ""
        targets = {o["target"]: o["status"] for o in reversed(m.get("outputs") or [])}
        outs = "  ".join(f"{t}:{'✓' if st == 'ok' else '✗'}" for t, st in targets.items())
        line = f"  {icon} {m['id']}  {title}{dur_s}"
        if outs:
            line += f"  [{outs}]"
        if m["status"] == "failed" and m.get("error"):
            line += f"\n      error: {m['error'].splitlines()[0][:100]}"
        print(line)
    return 0


# --- daemon management --------------------------------------------------------

def cmd_daemon(args) -> int:
    from .daemon import launchd

    if args.daemon_command == "serve":
        from .daemon.server import serve
        serve(port=args.port)
        return 0

    if args.daemon_command == "install":
        try:
            path = launchd.install()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"Installed LaunchAgent: {path}")
        print("scribed now runs in the background and restarts on login/crash.")
        print("\nNote: under launchd, macOS folder-privacy (TCC) prompts cannot appear.")
        print("If the index stays empty or recordings fail, grant your Python")
        print("(or Full Disk Access) permission to reach the recordings/notes folders,")
        print("or skip launchd and let the Meeting Scribe app start the daemon instead.")
        return 0

    if args.daemon_command == "uninstall":
        launchd.uninstall()
        print("LaunchAgent removed.")
        return 0

    if args.daemon_command == "start":
        try:
            launchd.start()
        except RuntimeError as e:
            print(f"Error: {e} — is it installed? (scribe daemon install)", file=sys.stderr)
            return 1
        print("Daemon kickstarted.")
        return 0

    if args.daemon_command == "stop":
        launchd.stop()
        print("Daemon stopped (launchd will restart it unless you uninstall).")
        return 0

    if args.daemon_command == "status":
        print(f"launchd: {launchd.status()}")
        c = DaemonClient()
        if c.is_up():
            st = c.status()
            rec = st["session"]
            print(f"daemon:  up (v{st['daemon_version']}) at {c.base}")
            if rec:
                print(f"         ● recording {rec['meeting_id']} ({rec['elapsed_sec']}s)")
            if st["active_job"]:
                j = st["active_job"]
                print(f"         job #{j['id']} {j['type']} {j['meeting_id']}: {j['phase']}")
        else:
            print("daemon:  not reachable")
        return 0

    print(f"Unknown daemon command: {args.daemon_command}", file=sys.stderr)
    return 1


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
    if "gdrive" in outs or "gdocs" in outs:
        gc = bool(cfg.google_client_id and cfg.google_client_secret)
        connected = cfg.google_token_path.is_file()
        print(f"    {_ok(gc)} google oauth client   {_ok(connected)} google connected "
              f"{'' if connected else '(scribe google connect)'}")

    c = DaemonClient()
    up = c.is_up()
    print(f"  {_ok(up)} daemon: {'reachable at ' + c.base if up else 'not running (scribe daemon install)'}")

    print("\nNote: system-audio capture needs Screen Recording permission for whichever\n"
          "process spawns it — your terminal when using --local, the daemon's parent\n"
          "when using scribed (System Settings > Privacy & Security > Screen Recording).")
    return 0


# --- google ------------------------------------------------------------------

def cmd_google(args) -> int:
    cfg = _load(args)
    if args.google_command == "connect":
        from .integrations import google_auth
        try:
            info = google_auth.connect_interactive(cfg)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"✓ Google connected: {info.get('email') or 'ok'}")
        print(f"  token: {cfg.google_token_path}")
        return 0
    if args.google_command == "disconnect":
        cfg.google_token_path.unlink(missing_ok=True)
        print("Google token removed.")
        return 0
    print(f"Unknown google command: {args.google_command}", file=sys.stderr)
    return 1


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

    ps = sub.add_parser("start", help="Begin recording")
    ps.add_argument("--local", action="store_true", help="Bypass the daemon")
    ps.set_defaults(func=cmd_start)

    pt = sub.add_parser("stop", help="Stop recording and process")
    pt.add_argument("--local", action="store_true", help="Bypass the daemon")
    pt.set_defaults(func=cmd_stop)

    pp = sub.add_parser("process", help="Process an existing recording/audio file")
    pp.add_argument("path", help="Recording base path, audio file, or meeting id")
    pp.add_argument("--local", action="store_true", help="Bypass the daemon")
    pp.set_defaults(func=cmd_process)

    pl = sub.add_parser("list", help="List captured meetings")
    pl.add_argument("-n", "--limit", type=int, default=25)
    pl.add_argument("-q", "--query", help="Search title/summary")
    pl.add_argument("--local", action="store_true", help="Bypass the daemon")
    pl.set_defaults(func=cmd_list)

    pd = sub.add_parser("daemon", help="Manage the scribed background daemon")
    pd.add_argument("daemon_command",
                    choices=["install", "uninstall", "start", "stop", "status", "serve"])
    pd.add_argument("--port", type=int, default=None, help="Port for `serve`")
    pd.set_defaults(func=cmd_daemon)

    pg = sub.add_parser("google", help="Connect/disconnect Google (Docs & Drive outputs)")
    pg.add_argument("google_command", choices=["connect", "disconnect"])
    pg.set_defaults(func=cmd_google)

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
