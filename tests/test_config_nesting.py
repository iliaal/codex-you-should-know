"""Real TOML-limit failures in temporary configuration only; no reviewer calls."""

import io
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from you_should_know import backend, cli, shortcuts
from you_should_know.config import Config, ConfigError, load_config
from you_should_know.state import State

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["array", "inline_table"])
def nested_toml(request):
    if request.param == "array":
        return "enabled = " + "[" * 2000 + "true" + "]" * 2000 + "\n"
    return "model = " + "{x=" * 2000 + "1" + "}" * 2000 + "\n"


def test_nested_config_becomes_config_error(tmp_path, nested_toml):
    path = tmp_path / "config.toml"
    path.write_text(nested_toml)
    with pytest.raises(RecursionError):
        tomllib.loads(nested_toml)
    with pytest.raises(ConfigError, match="Cannot read") as caught:
        load_config(tmp_path)
    assert str(path) in str(caught.value)
    assert isinstance(caught.value.__cause__, RecursionError)
    assert path.read_text() == nested_toml


@pytest.mark.parametrize("command", ["hook", "notify"])
@pytest.mark.parametrize("event_name", ["PostToolUse", "Stop", "UserPromptSubmit"])
def test_nested_config_hooks_warn_once_and_recover(
    tmp_path, nested_toml, monkeypatch, capsys, command, event_name
):
    path = tmp_path / "config.toml"
    path.write_text(nested_toml)
    monkeypatch.delenv("YSK_REVIEWER_ACTIVE", raising=False)
    monkeypatch.setattr(cli, "noninteractive_host", lambda: False)

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid configuration must not start a check or reviewer")

    monkeypatch.setattr(cli, "check", unexpected)
    monkeypatch.setattr(backend, "review", unexpected)

    def invoke(session, selected=command):
        event = {"session_id": session, "hook_event_name": event_name}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
        assert cli.main(["--data-dir", str(tmp_path), selected]) == 0
        output = capsys.readouterr()
        assert not output.err
        assert "Traceback" not in output.out
        return json.loads(output.out)

    assert str(path) in invoke("first")["systemMessage"]
    assert invoke("first", "notify" if command == "hook" else "hook") == {}
    assert invoke("first") == {}
    assert "systemMessage" in invoke("second")
    path.write_text(nested_toml + "# new revision\n")
    assert "systemMessage" in invoke("first")
    assert invoke("first") == {}
    state = State(tmp_path)
    try:
        assert state.diagnostics()["model_calls"] == 0
        assert state.diagnostics()["recent_checks"] == []
    finally:
        state.close()
    path.write_text("enabled = false\n")
    assert invoke("first") == {}
    assert load_config(tmp_path).enabled is False


@pytest.mark.parametrize("command", ["status", "hook", "notify"])
def test_nested_config_cli_has_no_traceback(tmp_path, nested_toml, command):
    (tmp_path / "config.toml").write_text(nested_toml)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/you_should_know.py"),
            "--data-dir",
            str(tmp_path),
            command,
        ],
        input=json.dumps({"session_id": "synthetic", "hook_event_name": "Stop"}),
        env={**os.environ, "YSK_REVIEWER_ACTIVE": "0"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == (1 if command == "status" else 0)
    assert "Traceback" not in result.stderr
    if command == "status":
        assert not result.stdout
        assert result.stderr.startswith("you-should-know: Cannot read")
    else:
        assert not result.stderr
        response = json.loads(result.stdout)
        # Noninteractive hosts intentionally suppress config warnings.
        assert response == {} or response["systemMessage"].startswith(
            "You should know: Cannot read"
        )


@pytest.fixture
def nesting_limit():
    original = sys.getrecursionlimit()
    sys.setrecursionlimit(600)
    try:
        yield
    finally:
        sys.setrecursionlimit(original)


@pytest.mark.parametrize(
    ("shape", "action"),
    [(shape, action) for shape in ("array", "inline_table") for action in ("install", "remove")]
    + [("deep_header", "remove")],
)
def test_shortcuts_nesting_failure_never_writes(
    tmp_path, action, shape, monkeypatch, capsys, nesting_limit
):
    path = tmp_path / "config.toml"
    if shape == "deep_header":
        # Parsing succeeds, but deep-copy of the nested table state hits a limit.
        if action == "remove":
            shortcuts.configure_shortcuts("install", path)
        original = path.read_text() if path.exists() else ""
        original += "[" + ".".join(["a"] * 650) + "]\nx = 1\n"
        assert isinstance(tomllib.loads(original), dict)
    else:
        original = (
            "x = " + "[" * 2000 + "1" + "]" * 2000 + "\n"
            if shape == "array"
            else "x = " + "{x=" * 2000 + "1" + "}" * 2000 + "\n"
        )
        with pytest.raises(RecursionError):
            tomllib.loads(original)
    path.write_text(original)
    path.chmod(0o640)
    before = path.stat()

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid configuration must never reach the write operation")

    monkeypatch.setattr(shortcuts, "_write", unexpected)
    with pytest.raises(ValueError, match="TOML nesting") as caught:
        shortcuts.configure_shortcuts(action, path)
    assert isinstance(caught.value.__cause__, RecursionError)
    assert str(path) in str(caught.value)
    assert cli.main(["shortcuts", action, "--config", str(path)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert output.err.startswith("you-should-know: Cannot process")
    assert "Traceback" not in output.err
    after = path.stat()
    assert (after.st_mtime_ns, after.st_mode, after.st_ino) == (
        before.st_mtime_ns,
        before.st_mode,
        before.st_ino,
    )
    assert path.read_text() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml"]


def test_shallow_configs_and_shortcuts_are_unchanged(tmp_path):
    assert load_config(tmp_path) == Config()
    path = tmp_path / "config.toml"
    path.write_text('enabled = false\nreview_instructions = "Keep Ω literal"\n')
    assert load_config(tmp_path) == Config(enabled=False, review_instructions="Keep Ω literal")
    path.write_text("[custom.deep.but.shallow]\nvalues = [[1, 2]]\n")
    original = path.read_bytes()
    assert shortcuts.configure_shortcuts("install", path)["status"] == "installed"
    assert shortcuts.configure_shortcuts("install", path)["status"] == "already_configured"
    assert shortcuts.configure_shortcuts("remove", path)["status"] == "removed"
    assert path.read_bytes() == original


@pytest.mark.parametrize("target", ["config", "shortcuts"])
def test_unrelated_runtime_errors_are_not_hidden(tmp_path, monkeypatch, target):
    path = tmp_path / "config.toml"
    path.write_text("enabled = false\n")

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic unrelated failure")

    if target == "config":
        monkeypatch.setattr(tomllib, "load", fail)
        call = lambda: load_config(tmp_path)  # noqa: E731
    else:
        monkeypatch.setattr(tomllib, "loads", fail)
        call = lambda: shortcuts.configure_shortcuts("install", path)  # noqa: E731
    with pytest.raises(RuntimeError, match="synthetic unrelated failure"):
        call()
