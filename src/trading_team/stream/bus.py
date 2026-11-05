"""
In-process pub/sub event bus for live regime / hedge / tick streaming.

Thread-safe, ring-buffered history per topic so late subscribers can catch up.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    topic: str
    payload: dict[str, Any]
    ts: str = field(default_factory=_utc)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    """Fan-out bus with per-topic ring buffers and subscriber queues."""

    TOPICS = (
        "regime",
        "hedge",
        "greeks",
        "tick",
        "quote",
        "alert",
        "feed_health",
        "surface",
        "var",
        "system",
    )

    def __init__(self, history: int = 200):
        self._lock = threading.RLock()
        self._seq = 0
        self._history: dict[str, deque[Event]] = {t: deque(maxlen=history) for t in self.TOPICS}
        self._subs: dict[str, list[tuple[str, Callable[[Event], None]]]] = {t: [] for t in self.TOPICS}
        self._queues: dict[str, deque[Event]] = {}  # sub_id -> queue
        self._queue_topics: dict[str, set[str]] = {}
        self._history_n = history

    def publish(self, topic: str, payload: dict[str, Any]) -> Event:
        if topic not in self._history:
            with self._lock:
                self._history[topic] = deque(maxlen=self._history_n)
                self._subs[topic] = []
        with self._lock:
            self._seq += 1
            ev = Event(topic=topic, payload=payload, seq=self._seq)
            self._history[topic].append(ev)
            # Push to matching subscriber queues
            for sub_id, topics in list(self._queue_topics.items()):
                if topic in topics or "*" in topics:
                    q = self._queues.get(sub_id)
                    if q is not None:
                        q.append(ev)
            # Fire callbacks (outside critical path copy)
            cbs = list(self._subs.get(topic, []))
        for _, cb in cbs:
            try:
                cb(ev)
            except Exception:
                pass
        return ev

    def subscribe_callback(self, topic: str, callback: Callable[[Event], None]) -> str:
        sub_id = uuid.uuid4().hex[:10]
        with self._lock:
            self._subs.setdefault(topic, []).append((sub_id, callback))
        return sub_id

    def unsubscribe_callback(self, topic: str, sub_id: str) -> None:
        with self._lock:
            self._subs[topic] = [(s, c) for s, c in self._subs.get(topic, []) if s != sub_id]

    def open_queue(self, topics: Iterable[str], *, backlog: int = 50) -> str:
        """Create a subscriber queue; returns sub_id for poll/close."""
        sub_id = uuid.uuid4().hex[:10]
        topic_set = set(topics) or {"*"}
        with self._lock:
            q: deque[Event] = deque(maxlen=max(backlog * 2, 100))
            # Seed with recent history
            for t in topic_set:
                if t == "*":
                    for hist in self._history.values():
                        for ev in list(hist)[-backlog:]:
                            q.append(ev)
                elif t in self._history:
                    for ev in list(self._history[t])[-backlog:]:
                        q.append(ev)
            self._queues[sub_id] = q
            self._queue_topics[sub_id] = topic_set
        return sub_id

    def poll(self, sub_id: str, max_items: int = 50) -> list[Event]:
        with self._lock:
            q = self._queues.get(sub_id)
            if q is None:
                return []
            out = []
            while q and len(out) < max_items:
                out.append(q.popleft())
            return out

    def wait(self, sub_id: str, timeout: float = 15.0, max_items: int = 50) -> list[Event]:
        """Block until at least one event or timeout (long-poll helper)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            items = self.poll(sub_id, max_items=max_items)
            if items:
                return items
            time.sleep(0.05)
        return []

    def close_queue(self, sub_id: str) -> None:
        with self._lock:
            self._queues.pop(sub_id, None)
            self._queue_topics.pop(sub_id, None)

    def history(self, topic: str, n: int = 50) -> list[Event]:
        with self._lock:
            return list(self._history.get(topic, []))[-n:]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "seq": self._seq,
                "topics": {t: len(h) for t, h in self._history.items()},
                "subscribers": {t: len(s) for t, s in self._subs.items()},
                "queues": len(self._queues),
            }


_BUS: EventBus | None = None
_BUS_LOCK = threading.Lock()


def get_bus() -> EventBus:
    global _BUS
    if _BUS is None:
        with _BUS_LOCK:
            if _BUS is None:
                _BUS = EventBus()
    return _BUS
