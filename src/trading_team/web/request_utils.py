"""Small request parsing helpers kept independent of the runtime and HTTP server."""
from __future__ import annotations

import json
from typing import Any


def read_json_body(handler: Any) -> dict:
    """Read a JSON object once, or reuse the object cached by ``do_POST``.

    ``BaseHTTPRequestHandler.rfile`` is a forward-only stream. If a POST handler
    delegates to a shared GET implementation, re-reading Content-Length bytes
    consumes an already exhausted stream and loses all request parameters.
    """
    if getattr(handler, "_parsed_json_body_available", False):
        cached = getattr(handler, "_parsed_json_body", {})
        return cached if isinstance(cached, dict) else {}

    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError, AttributeError):
        length = 0
    if length <= 0:
        return {}
    try:
        raw = handler.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
