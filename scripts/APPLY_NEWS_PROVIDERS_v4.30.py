from __future__ import annotations

from pathlib import Path
import importlib.util
import sys


def load_patch(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load installer: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    root = Path(__file__).resolve().parent
    app_path = root / "src/trading_team/web/app.py"
    if not app_path.exists():
        print("ERROR: Run this file from the project root (src/trading_team/web/app.py not found).", file=sys.stderr)
        return 2
    app = app_path.read_text(encoding="utf-8", errors="replace")
    is_v428 = 'VERSION = "4.28.0"' in app
    is_v429 = 'VERSION = "4.29.0"' in app
    is_v430 = 'VERSION = "4.30.0"' in app
    if is_v428:
        # The bundle carries the previous TradingNews.press adapter because the free-source installer
        # depends on its established v4.29 News Hub hooks. Applying both is guarded and backed up.
        patch = load_patch(root / "apply_tradingnews_press_v429.py", "_patch_tradingnews_v429")
        result = patch.main()
        if result:
            return result
        app = app_path.read_text(encoding="utf-8", errors="replace")
        is_v429 = 'VERSION = "4.29.0"' in app
    if is_v429:
        patch = load_patch(root / "apply_free_news_v430.py", "_patch_free_news_v430")
        return int(patch.main())
    if is_v430:
        if (root / "src/trading_team/data/free_news_sources.py").exists() and "free_news_sources" in (root / "src/trading_team/data/news_rss.py").read_text(encoding="utf-8", errors="replace"):
            print("v4.30 free news providers already appear to be installed; no changes made.")
            return 0
        print("ERROR: app.py is v4.30 but provider wiring is incomplete. Restore from .runtime/patch_backups or inspect manually.", file=sys.stderr)
        return 2
    print("ERROR: This installer supports v4.28.0 (combined v4.29 + v4.30) or v4.29.0 project roots only.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
