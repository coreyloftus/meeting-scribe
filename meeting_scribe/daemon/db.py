"""SQLite meetings index. Written only by the daemon; a rebuildable CACHE.

The durable truth is the filesystem — wav/log/transcript/notes files in the
recordings dir and the markdown notes in the output dir. `rebuild_from_disk`
repopulates the index by scanning those, so deleting meetings.db loses nothing.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..config import Config, expand
from .. import audio

STATUSES = ("recording", "recorded", "queued", "transcribing", "summarizing",
            "writing_outputs", "done", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
  id              TEXT PRIMARY KEY,      -- recording stamp, e.g. 2026-07-06_10-00-00
  base_path       TEXT NOT NULL,
  started_at      TEXT,
  ended_at        TEXT,
  status          TEXT NOT NULL,
  title           TEXT,
  slug            TEXT,
  duration_sec    INTEGER,
  system_wav      TEXT,
  mic_wav         TEXT,
  transcript_path TEXT,
  notes_path      TEXT,
  summary_md      TEXT,
  error           TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outputs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  target     TEXT NOT NULL,
  status     TEXT NOT NULL,              -- ok | failed
  url        TEXT,
  detail     TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_started ON meetings(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_outputs_meeting ON outputs(meeting_id);
"""

STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stamp_to_iso(stamp: str) -> str | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S").isoformat(timespec="seconds")
    except ValueError:
        return None


class Database:
    """Small thread-safe wrapper; one connection guarded by a lock (the daemon
    has exactly two writers: the request handlers and the job worker)."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # --- meetings ------------------------------------------------------------

    def upsert_meeting(self, id: str, **fields) -> None:
        with self._lock:
            now = _now()
            row = self._conn.execute("SELECT id FROM meetings WHERE id=?", (id,)).fetchone()
            if row is None:
                fields.setdefault("status", "recorded")
                fields.setdefault("base_path", "")
                cols = ["id", "created_at", "updated_at", *fields]
                vals = [id, now, now, *fields.values()]
                self._conn.execute(
                    f"INSERT INTO meetings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            else:
                sets = ", ".join(f"{k}=?" for k in fields)
                self._conn.execute(
                    f"UPDATE meetings SET {sets}, updated_at=? WHERE id=?",
                    [*fields.values(), now, id])
            self._conn.commit()

    def update_meeting(self, id: str, **fields) -> None:
        with self._lock:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._conn.execute(
                f"UPDATE meetings SET {sets}, updated_at=? WHERE id=?",
                [*fields.values(), _now(), id])
            self._conn.commit()

    def get_meeting(self, id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM meetings WHERE id=?", (id,)).fetchone()
        return dict(row) if row else None

    def list_meetings(self, limit: int = 100, offset: int = 0, q: str | None = None) -> list[dict]:
        sql = "SELECT * FROM meetings"
        args: list = []
        if q:
            sql += " WHERE title LIKE ? OR slug LIKE ? OR summary_md LIKE ? OR id LIKE ?"
            like = f"%{q}%"
            args += [like, like, like, like]
        sql += " ORDER BY COALESCE(started_at, id) DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def delete_meeting(self, id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM meetings WHERE id=?", (id,))
            self._conn.commit()

    def meetings_in_status(self, *statuses: str) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM meetings WHERE status IN ({marks})", statuses).fetchall()
        return [dict(r) for r in rows]

    # --- outputs -------------------------------------------------------------

    def add_output(self, meeting_id: str, target: str, ok: bool,
                   url: str | None, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO outputs (meeting_id, target, status, url, detail, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (meeting_id, target, "ok" if ok else "failed", url, detail, _now()))
            self._conn.commit()

    def outputs_for(self, meeting_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outputs WHERE meeting_id=? ORDER BY created_at DESC",
                (meeting_id,)).fetchall()
        return [dict(r) for r in rows]

    # --- rebuild from disk ---------------------------------------------------

    def rebuild_from_disk(self, cfg: Config) -> int:
        """Scan the recordings dir (and markdown output dir) and add any
        meetings the index doesn't know about. Never overwrites existing rows.
        Returns the number of meetings added."""
        rec_dir = cfg.recordings_dir
        if not rec_dir or not rec_dir.is_dir():
            return 0

        bases: dict[str, dict] = {}
        for f in rec_dir.iterdir():
            name = f.name
            for suffix, key in ((".system.wav", "system_wav"), (".mic.wav", "mic_wav"),
                                (".transcript.txt", "transcript_path"), (".notes.md", "notes_path")):
                if name.endswith(suffix):
                    stamp = name[:-len(suffix)]
                    if STAMP_RE.match(stamp):
                        bases.setdefault(stamp, {})[key] = str(f)
                    break

        added = 0
        for stamp, files in sorted(bases.items()):
            if self.get_meeting(stamp) is not None:
                continue
            wav = files.get("system_wav") or files.get("mic_wav")
            duration = audio.probe_duration_sec(Path(wav)) if wav else None
            self.upsert_meeting(
                stamp,
                base_path=str(rec_dir / stamp),
                started_at=stamp_to_iso(stamp),
                status="recorded",
                duration_sec=duration,
                **files,
            )
            added += 1

        self._link_markdown_outputs(cfg)
        return added

    def _link_markdown_outputs(self, cfg: Config) -> None:
        """Best-effort: pair `<date>-<slug>.md` notes with meetings by date.
        Only links when the date has exactly one meeting and one note file, so
        we never mis-attribute; ambiguous days stay `recorded` and can be
        reprocessed."""
        md_dir = expand(cfg.get("outputs", "markdown", "dir",
                                default="~/Documents/meeting-transcripts"))
        if not md_dir or not md_dir.is_dir():
            return

        notes_by_date: dict[str, list[Path]] = {}
        for f in md_dir.glob("*.md"):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$", f.name)
            if m:
                notes_by_date.setdefault(m.group(1), []).append(f)

        pending = [m for m in self.meetings_in_status("recorded") if not m["summary_md"]]
        by_date: dict[str, list[dict]] = {}
        for m in pending:
            by_date.setdefault(m["id"][:10], []).append(m)

        for date, meetings in by_date.items():
            notes = notes_by_date.get(date, [])
            if len(meetings) != 1 or len(notes) != 1:
                continue
            meeting, note_file = meetings[0], notes[0]
            slug = note_file.stem[11:]  # strip "YYYY-MM-DD-"
            try:
                text = note_file.read_text()
            except OSError:
                continue
            summary = _extract_summary(text)
            fields: dict = dict(
                status="done",
                slug=slug,
                title=f"{date} {slug.replace('-', ' ')}",
                summary_md=summary or None,
            )
            # The transcript must exist as its own durable file, or a later
            # re-push would rebuild the note with an empty transcript and
            # overwrite this one. Extract it back out of the note.
            if not meeting.get("transcript_path") and meeting.get("base_path"):
                transcript = _extract_section(text, "## Full Transcript")
                if transcript:
                    tp = Path(meeting["base_path"] + ".transcript.txt")
                    try:
                        if not tp.is_file():
                            tp.write_text(transcript)
                        fields["transcript_path"] = str(tp)
                    except OSError:
                        pass
            self.update_meeting(meeting["id"], **fields)
            self.add_output(meeting["id"], "markdown", True, str(note_file),
                            detail="linked on index rebuild")


def _extract_section(full_md: str, heading: str) -> str:
    """Text under `heading` (to the next same-level heading or EOF)."""
    idx = full_md.find(f"\n{heading}")
    if idx == -1:
        return ""
    body = full_md[idx + len(heading) + 1:]
    nxt = body.find("\n## ")
    if nxt != -1:
        body = body[:nxt]
    return body.strip()


def _extract_summary(full_md: str) -> str:
    """Pull the summary section back out of a full note file (everything after
    the metadata lines, before the My Notes / Full Transcript sections)."""
    lines = full_md.splitlines()
    body_start = 0
    for i, line in enumerate(lines[:8]):
        if line.startswith("_Date:") or line.startswith("_Audio:") or line.startswith("# "):
            body_start = i + 1
    body = "\n".join(lines[body_start:])
    for marker in ("\n## My Notes", "\n## Full Transcript"):
        idx = body.find(marker)
        if idx != -1:
            body = body[:idx]
    return body.strip().removesuffix("---").strip()
