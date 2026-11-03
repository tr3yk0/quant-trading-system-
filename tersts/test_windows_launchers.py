from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_only_three_batch_launchers_are_shipped():
    batches = sorted(p.name.lower() for p in ROOT.rglob("*.bat") if ".venv" not in p.parts)
    assert batches == ["debug.bat", "start.bat", "stop.bat"]


def test_start_installs_all_python_project_extras_and_quantlib():
    text = (ROOT / "START.bat").read_text(encoding="utf-8")
    assert 'pip install -e ".[dev,ml,quant,finance]"' in text
    assert '"QuantLib==1.44"' in text
    assert "MetaTrader5" in text
    assert "winget install --id Python.Python.3.12" in text
    assert "dependencies.sha256" in text
    assert "native_build_attempted.sha256" in text
    assert "logs\\install.log" in text
    # Hash commands are redirected to temp files to avoid FOR /F parsing errors
    # when the extracted project path contains spaces (e.g. "AI Trading Team").
    assert "dependencies_hash.tmp" in text
    assert "native_hash.tmp" in text
    assert not any("in (`\"%VPY%\" -c" in line for line in text.splitlines())


def test_stop_does_not_kill_processes_based_on_port_only():
    text = (ROOT / "STOP.bat").read_text(encoding="utf-8")
    assert "trading_team\\.monitor\\.guardian" in text
    assert "CommandLine.Contains($env:AI_TRADING_PROJECT_ROOT)" in text
    assert "taskkill /PID !GUARDIAN_PID! /T /F" in text
    assert "netstat -ano" not in text.lower()


def test_debug_uses_same_setup_but_foreground_guardian():
    start = (ROOT / "START.bat").read_text(encoding="utf-8")
    debug = (ROOT / "DEBUG.bat").read_text(encoding="utf-8")
    assert '"%VPY%" -u -m trading_team.monitor.guardian' in start
    assert 'call "%~dp0START.bat" --debug' in debug
    assert "AI_TRADING_DEBUG_WRAPPER=1" in debug

def test_start_supports_namespace_package_without_package_dunder_file():
    text = (ROOT / "START.bat").read_text(encoding="utf-8")
    assert "importlib.util.find_spec('trading_team.main')" in text
    assert "spec.origin" in text
    assert "trading_team.__file__" not in text



def test_start_does_not_use_fragile_nested_cmd_self_relaunch_and_pauses():
    text = (ROOT / "START.bat").read_text(encoding="utf-8")
    assert "cmd.exe /d /k" not in text
    assert "Press any key to close this launcher window" in text
    assert "Press any key to close this error window" in text
    assert "AI TRADING TEAM v4.39.0 - INSTITUTIONAL QUANT TERMINAL" in text


def test_start_python_version_matches_project_requirement():
    text = (ROOT / "START.bat").read_text(encoding="utf-8")
    assert "sys.version_info >= (3,12)" in text
    assert "py -3.13" in text
    assert "sys.version_info >= (3,11)" not in text
