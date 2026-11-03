#!/usr/bin/env python3
"""Probe local Ollama and the trading_team provider factory."""
from __future__ import annotations
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Load .env if present (simple parser)
env_path = ROOT / ".env"
if env_path.is_file():
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def check_ollama(base: str = "http://127.0.0.1:11434") -> dict:
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=5) as r:
            data = json.loads(r.read().decode())
        models = [m.get("name") for m in data.get("models") or []]
        return {"ok": True, "models": models}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main() -> int:
    print("=== Ollama probe ===")
    raw = check_ollama()
    print("  ollama /api/tags:", raw)
    if not raw.get("ok"):
        print("  Start Ollama first (ollama serve).")
        return 1

    # Force enable for this process if user has models
    os.environ["LOCAL_AI_ENABLED"] = os.environ.get("LOCAL_AI_ENABLED") or "true"
    os.environ.setdefault("LOCAL_AI_BASE_URL", "http://127.0.0.1:11434/v1")
    os.environ.setdefault("LOCAL_AI_API_KEY", "ollama")
    os.environ.setdefault("LOCAL_AI_TIERED", "true")
    os.environ.setdefault("LOCAL_AI_GATE_MODEL", "qwen2.5:1.5b")
    os.environ.setdefault("LOCAL_AI_DEEP_MODEL", "qwen2.5:3b")
    os.environ.setdefault("LOCAL_AI_MODEL", "qwen2.5:3b")

    from trading_team.ai_mesh.provider import create_provider

    p = create_provider()
    st = p.status()
    print("=== Provider factory ===")
    print(json.dumps(st, indent=2, default=str))

    if not st.get("configured") and p.name == "deterministic":
        print("  Still deterministic — set LOCAL_AI_ENABLED=true in .env")
        return 2

    print("=== Chat smoke test ===")
    try:
        ans = p.chat(
            "You are a research assistant. Reply in one short sentence.",
            "Say: Ollama linked to AI Trading Team.",
        )
        print("  response:", (ans or "")[:300])
    except Exception as exc:
        print("  chat failed:", exc)
        return 3

    print("=== Research copilot ===")
    try:
        from trading_team.research.copilot import ResearchCopilot
        copilot = ResearchCopilot(p)
        print("  status:", json.dumps(copilot.status(), indent=2, default=str))
    except Exception as exc:
        print("  copilot status failed:", exc)

    print("\nOK — local LLM is reachable. Decisions remain classical unless")
    print("LOCAL_AI_CAN_INFLUENCE_DECISION=true (not recommended until validated).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
