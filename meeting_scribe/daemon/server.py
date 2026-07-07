"""scribed HTTP server — the single source of truth for recording + meetings.

Run with `scribed serve` (or `scribe daemon serve`). Binds 127.0.0.1 only and
requires `Authorization: Bearer <token>` (token in ~/.local/state/meeting-scribe/
daemon.token) on every /v1 route.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config as config_mod
from .. import process as process_mod
from .. import recorder
from ..config import DEFAULT_USER_CONFIG
from ..outputs import Note, REGISTRY, write_one
from ..recorder import HELPER_BIN
from . import DAEMON_VERSION
from . import state
from .db import Database, stamp_to_iso
from .events import EventBus, sse_format
from .jobs import Job, JobQueue

BUS = EventBus()
JOBS = JobQueue()
DB: Database | None = None
TOKEN: str = ""

INTERRUPTED_STATUSES = ("queued", "transcribing", "summarizing", "writing_outputs")

# Secret config keys, as (path...) tuples: redacted on GET, preserved on PUT.
SECRET_PATHS = (
    ("anthropic", "api_key"),
    ("outputs", "notion", "token"),
    ("google", "client_secret"),
)
REDACTED = "•••"


def cfg():
    """Fresh config per use so PUT /config edits apply without a restart."""
    return config_mod.load()


# --- auth ---------------------------------------------------------------------

async def require_token(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    if not TOKEN or auth != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="missing or bad bearer token")


# --- serialization --------------------------------------------------------------

def meeting_public(m: dict, with_outputs: bool = True) -> dict:
    out = dict(m)
    if with_outputs and DB is not None:
        out["outputs"] = DB.outputs_for(m["id"])
    return out


def meeting_detail(m: dict) -> dict:
    out = meeting_public(m)
    out["transcript"] = _read_optional(m.get("transcript_path"))
    out["user_notes"] = _read_optional(m.get("notes_path"))
    return out


def _read_optional(path: str | None) -> str | None:
    if path and Path(path).is_file():
        try:
            return Path(path).read_text()
        except OSError:
            return None
    return None


def _session_public() -> dict | None:
    s = recorder.load_session()
    if not s or not recorder.is_recording():
        return None
    elapsed = None
    try:
        started = datetime.fromisoformat(s.started_at)
        elapsed = int((datetime.now() - started).total_seconds())
    except ValueError:
        pass
    return {
        "meeting_id": Path(s.base).name,
        "started_at": s.started_at,
        "elapsed_sec": elapsed,
        "system_wav": s.system_wav,
        "mic_wav": s.mic_wav,
    }


def _note_from_meeting(m: dict) -> Note:
    """Rebuild a Note from indexed data + persisted files, for push jobs."""
    if not m.get("summary_md"):
        raise HTTPException(status_code=409,
                            detail="meeting has no summary yet — process it first")
    slug = m.get("slug") or "meeting-notes"
    date = (m.get("started_at") or m["id"])[:10]
    return Note(
        title=m.get("title") or f"{date} {slug.replace('-', ' ')}",
        date=date,
        slug=slug,
        summary_md=m["summary_md"],
        transcript=_read_optional(m.get("transcript_path")) or "",
        audio_path=(m.get("base_path") or None) and m["base_path"] + ".*.wav",
        user_notes=_read_optional(m.get("notes_path")),
    )


# --- job handlers ----------------------------------------------------------------

def _handle_process(job: Job) -> None:
    m = DB.get_meeting(job.meeting_id)
    if m is None:
        raise RuntimeError(f"unknown meeting {job.meeting_id}")
    if not (m.get("system_wav") or m.get("mic_wav")):
        raise RuntimeError("meeting has no audio files on disk")

    def on_phase(phase: str, pct: float | None) -> None:
        job.phase, job.pct = phase, pct
        DB.update_meeting(job.meeting_id, status=phase)
        BUS.publish("job_progress", job_id=job.id, meeting_id=job.meeting_id,
                    phase=phase, pct=pct)

    try:
        result = process_mod.process(
            cfg(), m.get("system_wav"), m.get("mic_wav"),
            audio_label=(m.get("base_path") or "") + ".*.wav",
            meeting_date=(m.get("started_at") or m["id"])[:10],
            on_phase=on_phase)
    except Exception as e:
        DB.update_meeting(job.meeting_id, status="failed", error=str(e))
        BUS.publish("meeting_updated", meeting_id=job.meeting_id, status="failed",
                    error=str(e))
        raise

    note = result.note
    DB.update_meeting(
        job.meeting_id, status="done", error=None,
        title=note.title, slug=note.slug, summary_md=note.summary_md,
        transcript_path=str(result.transcript_path) if result.transcript_path else None)
    for r in result.outputs:
        DB.add_output(job.meeting_id, r.target, r.ok, r.url, r.detail)
        BUS.publish("output_pushed", meeting_id=job.meeting_id, target=r.target,
                    ok=r.ok, url=r.url, detail=r.detail)
    BUS.publish("meeting_updated", meeting_id=job.meeting_id, status="done")


def _handle_push(job: Job) -> None:
    m = DB.get_meeting(job.meeting_id)
    if m is None:
        raise RuntimeError(f"unknown meeting {job.meeting_id}")
    note = _note_from_meeting(m)
    target = job.options.get("target", "")
    r = write_one(cfg(), note, target, job.options.get("options"))
    DB.add_output(job.meeting_id, r.target, r.ok, r.url, r.detail)
    BUS.publish("output_pushed", meeting_id=job.meeting_id, target=r.target,
                ok=r.ok, url=r.url, detail=r.detail)
    BUS.publish("meeting_updated", meeting_id=job.meeting_id, status=m["status"])
    if not r.ok:
        raise RuntimeError(f"push to {target} failed: {r.detail}")


JOBS.register("process", _handle_process)
JOBS.register("reprocess", _handle_process)
JOBS.register("push", _handle_push)


# --- startup reconcile -------------------------------------------------------------

def reconcile(db: Database) -> None:
    added = db.rebuild_from_disk(cfg())
    if added:
        print(f"[scribed] index: added {added} meeting(s) from disk")

    # Interrupted jobs -> failed; the audio is safe and reprocess re-queues.
    for m in db.meetings_in_status(*INTERRUPTED_STATUSES):
        db.update_meeting(m["id"], status="failed",
                          error="daemon restarted while processing — audio is safe, retry")

    # A live recording survives a daemon restart; a dead one becomes `recorded`.
    s = recorder.load_session()
    if s is not None:
        mid = Path(s.base).name
        base = {"base_path": s.base, "started_at": s.started_at,
                "system_wav": s.system_wav, "mic_wav": s.mic_wav}
        if recorder.is_recording():
            db.upsert_meeting(mid, status="recording", **base)
            print(f"[scribed] resumed tracking live recording {mid}")
        else:
            db.upsert_meeting(mid, status="recorded",
                              ended_at=datetime.now().isoformat(timespec="seconds"), **base)
            recorder.SESSION_FILE.unlink(missing_ok=True)
            print(f"[scribed] found stale session {mid}; marked recorded")

    for m in db.meetings_in_status("recording"):
        if not recorder.is_recording() or Path(recorder.load_session().base).name != m["id"]:
            db.update_meeting(m["id"], status="recorded")


async def _ticker() -> None:
    while True:
        await asyncio.sleep(1)
        s = _session_public()
        if s:
            BUS.publish("tick", meeting_id=s["meeting_id"], elapsed_sec=s["elapsed_sec"])


# --- app ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    BUS.attach_loop(loop)
    JOBS.start()
    # Reconcile in a worker thread WITHOUT awaiting it: scanning the recordings
    # or notes folders can block indefinitely on a macOS folder-permission (TCC)
    # check — that must never keep /v1/status from answering. The index fills
    # in as soon as the scan completes (or the user grants access).
    loop.run_in_executor(None, _reconcile_safely)
    tick_task = asyncio.create_task(_ticker())
    yield
    tick_task.cancel()


def _reconcile_safely() -> None:
    try:
        reconcile(DB)
        print("[scribed] startup reconcile complete")
    except Exception as e:
        print(f"[scribed] startup reconcile failed: {e}")


app = FastAPI(title="scribed", version=DAEMON_VERSION, lifespan=lifespan,
              dependencies=[Depends(require_token)])


class PushBody(BaseModel):
    target: str
    options: dict | None = None


class NotesBody(BaseModel):
    notes: str


class ReprocessBody(BaseModel):
    options: dict | None = None


@app.get("/v1/status")
def get_status():
    active = JOBS.active()
    return {
        "recording": recorder.is_recording(),
        "session": _session_public(),
        "active_job": active.public() if active else None,
        "queued_jobs": [j.public() for j in JOBS.queued()],
        "daemon_version": DAEMON_VERSION,
    }


@app.get("/v1/events")
async def get_events():
    async def stream():
        q = BUS.subscribe()
        try:
            yield sse_format({"type": "hello", "status": get_status()})
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield sse_format(event)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            BUS.unsubscribe(q)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/v1/start")
def post_start():
    try:
        s = recorder.start(cfg())
    except recorder.RecorderError as e:
        code = 409 if "Already recording" in str(e) else 500
        raise HTTPException(status_code=code, detail=str(e))
    mid = Path(s.base).name
    DB.upsert_meeting(mid, base_path=s.base, started_at=s.started_at,
                      status="recording", system_wav=s.system_wav, mic_wav=s.mic_wav)
    BUS.publish("recording_started", meeting_id=mid, started_at=s.started_at)
    return {"meeting": meeting_public(DB.get_meeting(mid))}


@app.post("/v1/stop")
def post_stop():
    t0 = time.monotonic()
    try:
        s = recorder.stop(cfg())
    except recorder.RecorderError as e:
        raise HTTPException(status_code=409, detail=str(e))
    print(f"[scribed] captures stopped in {time.monotonic() - t0:.2f}s", flush=True)
    mid = Path(s.base).name
    ended = datetime.now().isoformat(timespec="seconds")
    duration = None
    try:
        duration = int((datetime.fromisoformat(ended)
                        - datetime.fromisoformat(s.started_at)).total_seconds())
    except ValueError:
        pass
    DB.upsert_meeting(mid, base_path=s.base, started_at=s.started_at,
                      ended_at=ended, duration_sec=duration, status="queued",
                      system_wav=s.system_wav, mic_wav=s.mic_wav)
    job = JOBS.submit("process", mid)
    BUS.publish("recording_stopped", meeting_id=mid, job_id=job.id)
    return {"meeting_id": mid, "job_id": job.id}


@app.get("/v1/meetings")
def list_meetings(limit: int = 100, offset: int = 0, q: str | None = None):
    return {"meetings": [meeting_public(m) for m in DB.list_meetings(limit, offset, q)]}


@app.get("/v1/meetings/{meeting_id}")
def get_meeting(meeting_id: str):
    m = DB.get_meeting(meeting_id)
    if m is None:
        raise HTTPException(status_code=404, detail="unknown meeting")
    return {"meeting": meeting_detail(m)}


@app.put("/v1/meetings/{meeting_id}/notes")
def put_meeting_notes(meeting_id: str, body: NotesBody):
    m = DB.get_meeting(meeting_id)
    if m is None:
        raise HTTPException(status_code=404, detail="unknown meeting")
    base = m.get("base_path") or ""
    if not base:
        raise HTTPException(status_code=409, detail="meeting has no base path")
    notes_path = process_mod.notes_path_for(Path(base))
    notes_path.parent.mkdir(parents=True, exist_ok=True)
    notes_path.write_text(body.notes)
    DB.update_meeting(meeting_id, notes_path=str(notes_path))
    BUS.publish("meeting_updated", meeting_id=meeting_id, status=m["status"])
    return {"ok": True, "notes_path": str(notes_path)}


@app.put("/v1/session/notes")
def put_session_notes(body: NotesBody):
    s = recorder.load_session()
    if s is None or not recorder.is_recording():
        raise HTTPException(status_code=409, detail="no active recording")
    return put_meeting_notes(Path(s.base).name, body)


@app.post("/v1/meetings/{meeting_id}/reprocess")
def post_reprocess(meeting_id: str, body: ReprocessBody | None = None):
    m = DB.get_meeting(meeting_id)
    if m is None:
        raise HTTPException(status_code=404, detail="unknown meeting")
    if m["status"] == "recording":
        raise HTTPException(status_code=409, detail="meeting is still recording")
    if not (m.get("system_wav") or m.get("mic_wav")):
        raise HTTPException(status_code=409, detail="no audio on disk for this meeting")
    DB.update_meeting(meeting_id, status="queued", error=None)
    job = JOBS.submit("reprocess", meeting_id,
                      (body.options if body else None) or {})
    BUS.publish("meeting_updated", meeting_id=meeting_id, status="queued")
    return {"job_id": job.id}


@app.post("/v1/meetings/{meeting_id}/push")
def post_push(meeting_id: str, body: PushBody):
    m = DB.get_meeting(meeting_id)
    if m is None:
        raise HTTPException(status_code=404, detail="unknown meeting")
    if body.target not in REGISTRY:
        raise HTTPException(status_code=400,
                            detail=f"unknown target {body.target!r} (known: {', '.join(REGISTRY)})")
    _note_from_meeting(m)  # 409s early if there is nothing to push
    job = JOBS.submit("push", meeting_id,
                      {"target": body.target, "options": body.options})
    return {"job_id": job.id}


@app.delete("/v1/meetings/{meeting_id}")
def delete_meeting(meeting_id: str, delete_files: bool = False):
    m = DB.get_meeting(meeting_id)
    if m is None:
        raise HTTPException(status_code=404, detail="unknown meeting")
    if m["status"] == "recording":
        raise HTTPException(status_code=409, detail="stop the recording first")
    removed: list[str] = []
    if delete_files and m.get("base_path"):
        base = m["base_path"]
        for f in Path(base).parent.glob(Path(base).name + ".*"):
            try:
                f.unlink()
                removed.append(str(f))
            except OSError:
                pass
    DB.delete_meeting(meeting_id)
    BUS.publish("meeting_updated", meeting_id=meeting_id, status="deleted")
    return {"ok": True, "deleted_files": removed}


# --- config / integrations / doctor -------------------------------------------------

def _get_path(d: dict, path: tuple) -> object:
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _set_path(d: dict, path: tuple, value) -> None:
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = value


def _del_path(d: dict, path: tuple) -> None:
    for k in path[:-1]:
        if not isinstance(d, dict) or k not in d:
            return
        d = d[k]
    if isinstance(d, dict):
        d.pop(path[-1], None)


@app.get("/v1/config")
def get_config():
    c = cfg()
    data = json.loads(json.dumps(c.data))  # deep copy
    for path in SECRET_PATHS:
        if _get_path(data, path):
            _set_path(data, path, REDACTED)
    return {"config": data, "source": str(c.source) if c.source else None}


@app.put("/v1/config")
def put_config(body: dict):
    c = cfg()
    target = c.source or DEFAULT_USER_CONFIG
    current: dict = {}
    if target.is_file():
        try:
            current = json.loads(target.read_text())
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail=f"{target} is not valid JSON")
    # Don't let the redaction placeholder overwrite a real secret.
    for path in SECRET_PATHS:
        if _get_path(body, path) == REDACTED:
            _del_path(body, path)
    merged = config_mod._deep_merge(current, body)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, indent=2) + "\n")
    return get_config()


@app.get("/v1/integrations")
def get_integrations():
    c = cfg()
    enabled = set(c.enabled_outputs())
    items = []
    for key, spec in REGISTRY.items():
        items.append({
            "key": key,
            "label": spec.label,
            "enabled": key in enabled,
            "configured": spec.is_configured(c),
        })
    google_connected = c.google_token_path.is_file()
    return {"outputs": items,
            "google": {"connected": google_connected,
                       "client_configured": bool(c.google_client_id and c.google_client_secret)}}


@app.post("/v1/integrations/google/connect")
def google_connect():
    c = cfg()
    if not (c.google_client_id and c.google_client_secret):
        raise HTTPException(status_code=409,
                            detail="set google.client_id and google.client_secret first "
                                   "(GCP Desktop-app OAuth client)")
    from ..integrations import google_auth
    try:
        info = google_auth.connect_interactive(c)
    except google_auth.GoogleAuthError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, **info}


@app.get("/v1/doctor")
def get_doctor():
    c = cfg()
    checks = []

    def check(key: str, ok: bool, detail: str):
        checks.append({"key": key, "ok": ok, "detail": detail})

    for tool in ("ffmpeg", "ffprobe", c.whisper_cli, "SwitchAudioSource"):
        path = shutil.which(tool)
        check(f"tool:{tool}", bool(path), path or "not found on PATH")
    check("helper:syscap", HELPER_BIN.exists(),
          str(HELPER_BIN) if HELPER_BIN.exists() else "not built — run scripts/build_helper.sh")
    model = c.whisper_model
    check("whisper_model", bool(model and Path(model).is_file()), str(model or "(unset)"))
    check("config", bool(c.source), str(c.source or "defaults only"))
    if c.llm_backend == "claude_cli":
        cli = shutil.which(c.claude_cli)
        check("llm", bool(cli), f"claude_cli backend ({cli or 'claude not on PATH'})")
    else:
        check("llm", bool(c.anthropic_key),
              "api backend" + ("" if c.anthropic_key else " — no ANTHROPIC_API_KEY"))
    for item in get_integrations()["outputs"]:
        if item["enabled"]:
            check(f"output:{item['key']}", item["configured"],
                  "configured" if item["configured"] else "enabled but not configured")
    check("index", DB is not None, f"{len(DB.list_meetings(limit=100000))} meetings indexed" if DB else "no db")
    check("screen_recording", True,
          "cannot be verified from here — if system capture dies instantly, grant "
          "Screen Recording to the process that runs scribed")
    return {"checks": checks, "daemon_version": DAEMON_VERSION}


# --- entry point ---------------------------------------------------------------------

def _pick_port(host: str, want: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, want))
            return want
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def serve(host: str = state.DEFAULT_HOST, port: int | None = None) -> None:
    global DB, TOKEN
    import uvicorn
    import signal as _signal
    # A daemon launched as a shell background job inherits SIGINT/SIGQUIT
    # ignored, and ignored dispositions survive exec — so ffmpeg/syscap would
    # never see our stop signal. Restore defaults before spawning anything.
    for _sig in (_signal.SIGINT, _signal.SIGQUIT):
        if _signal.getsignal(_sig) == _signal.SIG_IGN:
            _signal.signal(_sig, _signal.SIG_DFL)
            print(f"[scribed] reset inherited SIG_IGN for {_sig.name}", flush=True)
    TOKEN = state.ensure_token()
    DB = Database(state.DB_FILE)
    actual_port = _pick_port(host, port or state.DEFAULT_PORT)
    state.write_info(host, actual_port, DAEMON_VERSION)
    print(f"[scribed] v{DAEMON_VERSION} listening on http://{host}:{actual_port}")
    uvicorn.run(app, host=host, port=actual_port, log_level="info")


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="scribed", description="meeting-scribe daemon")
    sub = p.add_subparsers(dest="command")
    sp = sub.add_parser("serve", help="run the daemon (default)")
    sp.add_argument("--host", default=state.DEFAULT_HOST)
    sp.add_argument("--port", type=int, default=None)
    args = p.parse_args(argv)
    serve(host=getattr(args, "host", state.DEFAULT_HOST),
          port=getattr(args, "port", None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
