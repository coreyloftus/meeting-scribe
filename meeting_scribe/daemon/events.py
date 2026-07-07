"""In-process SSE event bus.

Publishers may be any thread (the job worker publishes progress from its worker
thread); subscribers are asyncio consumers on the uvicorn event loop. Cross-
thread delivery goes through `loop.call_soon_threadsafe`.

Event shape: {"type": <name>, ...payload}. Types used by the daemon:
    recording_started, recording_stopped, tick, job_progress,
    meeting_updated, output_pushed
"""
from __future__ import annotations

import asyncio
import json
import threading


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, type: str, **payload) -> None:
        """Thread-safe; drops events if no loop is attached yet (startup)."""
        event = {"type": type, **payload}
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer: drop rather than block the daemon


def sse_format(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
