"""Serial background job queue.

One worker thread drains jobs one at a time (transcription is CPU-bound and
whisper already uses all cores; parallel jobs would just thrash). Handlers are
registered per job type by the server; a handler failure marks the job failed
but never kills the worker.
"""
from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Callable


@dataclass
class Job:
    id: int
    type: str                    # process | reprocess | push
    meeting_id: str
    options: dict = field(default_factory=dict)
    status: str = "queued"       # queued | running | done | failed
    phase: str | None = None
    pct: float | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def public(self) -> dict:
        d = asdict(self)
        d.pop("options", None)
        return d


class JobQueue:
    def __init__(self) -> None:
        self._q: queue.Queue[Job] = queue.Queue()
        self._handlers: dict[str, Callable[[Job], None]] = {}
        self._jobs: dict[int, Job] = {}
        self._current: Job | None = None
        self._next_id = 1
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def register(self, type: str, handler: Callable[[Job], None]) -> None:
        self._handlers[type] = handler

    def submit(self, type: str, meeting_id: str, options: dict | None = None) -> Job:
        with self._lock:
            job = Job(id=self._next_id, type=type, meeting_id=meeting_id,
                      options=options or {})
            self._next_id += 1
            self._jobs[job.id] = job
        self._q.put(job)
        return job

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def active(self) -> Job | None:
        cur = self._current
        return cur if cur and cur.status == "running" else None

    def queued(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.status == "queued"]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="scribed-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._q.get()
            handler = self._handlers.get(job.type)
            self._current = job
            job.status = "running"
            try:
                if handler is None:
                    raise RuntimeError(f"no handler for job type {job.type!r}")
                handler(job)
                job.status = "done"
            except Exception as e:
                job.status = "failed"
                job.error = f"{e}\n{traceback.format_exc(limit=3)}"
            finally:
                self._current = None
                self._q.task_done()
