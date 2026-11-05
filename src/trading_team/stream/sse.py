"""
Server-Sent Events (SSE) helpers for live regime / hedge streaming.

Works with the stdlib HTTP servers already used by web.app and monitor.dashboard.
Clients: EventSource('/api/stream?topics=regime,hedge,alert')
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterable

from trading_team.stream.bus import Event, EventBus, get_bus


def format_sse(event: Event | None = None, *, data: Any = None, event_name: str | None = None,
               event_id: str | None = None, retry_ms: int | None = None) -> bytes:
    """Serialize one SSE frame."""
    lines: list[str] = []
    if retry_ms is not None:
        lines.append(f"retry: {int(retry_ms)}")
    name = event_name or (event.topic if event else None)
    eid = event_id or (event.id if event else None)
    if name:
        lines.append(f"event: {name}")
    if eid:
        lines.append(f"id: {eid}")
    payload = data if data is not None else (event.to_dict() if event else {})
    raw = json.dumps(payload, default=str, separators=(",", ":"))
    # Split multi-line safely
    for part in raw.split("\n"):
        lines.append(f"data: {part}")
    lines.append("")  # blank line terminates event
    lines.append("")
    return ("\n".join(lines)).encode("utf-8")


class SSEBroker:
    """
    Manages SSE client sessions against the shared EventBus.

    Usage inside an HTTP handler:
        broker = SSEBroker(topics=['regime','hedge'])
        for chunk in broker.stream(timeout=30):
            self.wfile.write(chunk); self.wfile.flush()
    """

    def __init__(
        self,
        topics: Iterable[str] | None = None,
        *,
        bus: EventBus | None = None,
        heartbeat_s: float = 10.0,
        backlog: int = 20,
    ):
        self.bus = bus or get_bus()
        self.topics = list(topics) if topics else ["regime", "hedge", "alert", "feed_health"]
        self.heartbeat_s = heartbeat_s
        self.sub_id = self.bus.open_queue(self.topics, backlog=backlog)
        self._closed = False

    def stream(self, *, max_seconds: float = 300.0):
        """Yield SSE byte chunks until max_seconds or client disconnect."""
        start = time.time()
        # Initial retry hint + hello
        yield format_sse(
            data={"type": "hello", "topics": self.topics, "sub_id": self.sub_id},
            event_name="system",
            retry_ms=3000,
        )
        last_beat = time.time()
        try:
            while not self._closed and (time.time() - start) < max_seconds:
                events = self.bus.wait(self.sub_id, timeout=1.0, max_items=32)
                if events:
                    for ev in events:
                        yield format_sse(ev)
                    last_beat = time.time()
                elif time.time() - last_beat >= self.heartbeat_s:
                    yield b": heartbeat\n\n"
                    last_beat = time.time()
        finally:
            self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.bus.close_queue(self.sub_id)
