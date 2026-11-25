"""
table_split.sse — Server-Sent Events (SSE) broadcaster for Table Split live synchronization.

Features:
- Thread-safe and async-loop-safe event delivery (`call_soon_threadsafe`).
- Per-session subscriber fan-out.
- Standard SSE wire-format serializer (`id`, `event`, `data`) with integer `seq` IDs for `Last-Event-ID`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any


@dataclass
class SSESubscriber:
    loop: asyncio.AbstractEventLoop | None
    queue: asyncio.Queue[dict[str, Any]]


def format_sse_frame(
    event_id: str | int,
    event_type: str,
    data: dict[str, Any],
) -> str:
    """Formats a single RFC-compliant Server-Sent Event frame."""
    payload_str = json.dumps(data, separators=(",", ":"), default=str)
    return f"id: {event_id}\nevent: {event_type}\ndata: {payload_str}\n\n"


class SSEHub:
    """Per-session pub/sub hub fanning out persisted session events to live SSE streams."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[SSESubscriber]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscriber_count(self, session_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(session_id, []))

    def publish(self, session_id: str, event: dict[str, Any]) -> int:
        """
        Publishes a sanitized event payload to all connected subscribers of `session_id`.
        Safe to call from synchronous route handlers / background threads.
        Returns number of subscribers notified.
        """
        with self._lock:
            subs = list(self._subscribers.get(session_id, []))

        delivered = 0
        for sub in subs:
            # Never let a disconnected subscriber abort publishing to others
            with suppress(asyncio.QueueFull, RuntimeError):
                if sub.loop is not None and not sub.loop.is_closed():
                    sub.loop.call_soon_threadsafe(sub.queue.put_nowait, event)
                else:
                    sub.queue.put_nowait(event)
                delivered += 1
        return delivered

    def register(self, session_id: str) -> SSESubscriber:
        """Registers a live subscriber queue on the active event loop."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        sub = SSESubscriber(loop=loop, queue=queue)
        with self._lock:
            self._subscribers[session_id].append(sub)
        return sub

    def unregister(self, session_id: str, sub: SSESubscriber) -> None:
        """Removes a registered subscriber cleanly on disconnect or setup error."""
        with self._lock:
            subs = self._subscribers.get(session_id)
            if subs and sub in subs:
                subs.remove(sub)
            if subs is not None and len(subs) == 0:
                self._subscribers.pop(session_id, None)

    @asynccontextmanager
    async def subscribe(self, session_id: str) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Async context manager registering and cleaning up a live subscriber queue."""
        sub = self.register(session_id)
        try:
            yield sub.queue
        finally:
            self.unregister(session_id, sub)
