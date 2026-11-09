"""Configuration-path fixtures only; no installed settings or reviewer calls."""

import errno
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from you_should_know import backend, cli
from you_should_know.config import Config, ConfigError, load_config
from you_should_know.state import State


@pytest.fixture(params=["dangling", "self", "mutual", "looping_target_parent"])
def broken_config(request, tmp_path):
    path = tmp_path / "config.toml"
    target = tmp_path / "target"
    if request.param == "self":
        target = path
    elif request.param == "mutual":
        target.symlink_to(path)
    elif request.param == "looping_target_parent":
        target.symlink_to(target, target_is_directory=True)
        target = target / "settings.toml"
    path.symlink_to(target)
    return path


def test_broken_config_link_is_not_missing_configuration(broken_config):
    with pytest.raises(ConfigError, match="Cannot read") as caught:
        load_config(broken_config.parent)
    assert str(broken_config) in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)


@pytest.mark.parametrize("command", ["status", "hook", "notify"])
def test_broken_config_link_reports_error_without_review(
    broken_config, monkeypatch, capsys, command
):
    monkeypatch.delenv("YSK_REVIEWER_ACTIVE", raising=False)
    monkeypatch.setattr(cli, "noninteractive_host", lambda: False)
    transcript = broken_config.parent / "rollout.jsonl"
    transcript.write_text('{"type":"session_meta","payload":{}}\n')
    event = {
        "session_id": "synthetic",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    def unexpected(*args, **kwargs):
        pytest.fail("Invalid configuration must not start a check or reviewer")

    monkeypatch.setattr(cli, "check", unexpected)
    monkeypatch.setattr(backend, "review", unexpected)
    assert cli.main(["--data-dir", str(broken_config.parent), command]) == (
        1 if command == "status" else 0
    )
    output = capsys.readouterr()
    if command == "status":
        assert not output.out
        assert output.err.startswith("you-should-know: Cannot read ")
    else:
        assert not output.err
        assert json.loads(output.out)["systemMessage"].startswith("You should know: Cannot read ")
        state = State(broken_config.parent)
        try:
            assert state.diagnostics()["model_calls"] == 0
            assert state.diagnostics()["recent_checks"] == []
        finally:
            state.close()
    assert broken_config.is_symlink()


@pytest.mark.parametrize("shape", ["missing_directory", "missing_file", "file", "link"])
def test_missing_and_readable_config_paths_remain_supported(tmp_path, shape):
    directory = tmp_path / "data"
    expected = Config()
    if shape != "missing_directory":
        directory.mkdir()
    if shape in {"file", "link"}:
        target = directory / "config.toml" if shape == "file" else tmp_path / "settings.toml"
        target.write_text('enabled = false\nmodel = "custom"\nmax_checks_per_hour = 5\n')
        if shape == "link":
            (directory / "config.toml").symlink_to(target)
        expected = replace(Config(), enabled=False, model="custom", max_checks_per_hour=5)
    assert load_config(directory) == expected


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("command", ["hook", "notify"])
def test_readable_disabled_config_still_disables_hooks(
    tmp_path, monkeypatch, capsys, linked, command
):
    directory = tmp_path / "data"
    directory.mkdir()
    path = directory / "config.toml"
    target = tmp_path / "settings.toml" if linked else path
    target.write_text("enabled = false\n")
    if linked:
        path.symlink_to(target)
    monkeypatch.delenv("YSK_REVIEWER_ACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"s","hook_event_name":"Stop"}'))

    def unexpected(*args, **kwargs):
        pytest.fail("Disabled configuration must not start a check or reviewer")

    monkeypatch.setattr(cli, "check", unexpected)
    monkeypatch.setattr(backend, "review", unexpected)
    assert cli.main(["--data-dir", str(directory), command]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {}
    assert not output.err
    assert not (directory / "state.sqlite3").exists()


@pytest.mark.parametrize("failure", [errno.EACCES, errno.ENOTDIR])
def test_config_lookup_failure_is_reported(tmp_path, monkeypatch, failure):
    original_lstat = Path.lstat
    path = tmp_path / "config.toml"

    def unavailable(candidate):
        if candidate == path:
            raise OSError(failure, "synthetic lookup failure")
        return original_lstat(candidate)

    monkeypatch.setattr(Path, "lstat", unavailable)
    with pytest.raises(ConfigError, match="Cannot read") as caught:
        load_config(tmp_path)
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == failure


def test_config_removed_after_lookup_is_reported(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("enabled = false\n")
    original_open = Path.open

    def disappeared(candidate, *args, **kwargs):
        if candidate == path:
            path.unlink()
        return original_open(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disappeared)
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(tmp_path)
