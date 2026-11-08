import errno
import io
import json
from pathlib import Path

import pytest

from you_should_know.cli import main
from you_should_know.config import data_directory


@pytest.fixture(params=["argument", "environment", "codex_home", "home"])
def data_path(request, tmp_path, monkeypatch):
    monkeypatch.delenv("YSK_DATA_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("YSK_REVIEWER_ACTIVE", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    source = request.param
    if source == "home":
        directory = tmp_path / ".codex" / "you-should-know"
    elif source == "codex_home":
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
        directory = tmp_path / "codex" / "you-should-know"
    else:
        directory = tmp_path / "data"
        if source == "environment":
            monkeypatch.setenv("YSK_DATA_DIR", str(directory))
    arguments = ["--data-dir", str(directory)] if source == "argument" else []
    return directory, arguments


@pytest.mark.parametrize("shape", ["self", "mutual", "ancestor"])
@pytest.mark.parametrize("command", ["status", "hook", "notify"])
def test_data_directory_loops_report_errors(
    data_path, tmp_path, monkeypatch, capsys, shape, command
):
    directory, arguments = data_path
    if shape == "ancestor":
        # Leave the final directory missing beneath a looping parent.
        parent = tmp_path / "loop"
        parent.symlink_to(parent, target_is_directory=True)
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.symlink_to(parent / "missing", target_is_directory=True)
    else:
        directory.parent.mkdir(parents=True, exist_ok=True)
        target = directory
        if shape == "mutual":
            target = tmp_path / "other"
            target.symlink_to(directory, target_is_directory=True)
        directory.symlink_to(target, target_is_directory=True)
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    event = {"session_id": "synthetic", "hook_event_name": "PostToolUse"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    assert main([*arguments, command]) == (1 if command == "status" else 0)
    output = capsys.readouterr()
    if command == "status":
        assert not output.out
        message = output.err
        assert message.startswith("you-should-know: Cannot resolve data directory: ")
    else:
        assert not output.err
        message = json.loads(output.out)["systemMessage"]
        assert message.startswith("You should know: Cannot resolve data directory: ")
    assert "loop" in message.lower() or "symbolic links" in message.lower()
    assert "Traceback" not in message
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("shape", ["missing", "directory", "symlink"])
def test_valid_data_paths_remain_supported(data_path, tmp_path, capsys, shape):
    directory, arguments = data_path
    if shape == "directory":
        directory.mkdir(parents=True)
    elif shape == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.symlink_to(target, target_is_directory=True)
    assert main([*arguments, "status"]) == 0
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out)["sessions"] == []
    assert (directory / "state.sqlite3").is_file()


@pytest.mark.parametrize("failure", [errno.ENOENT, errno.EACCES, errno.ENOTDIR])
def test_data_path_probe_does_not_reject_other_stat_errors(data_path, monkeypatch, failure):
    directory, arguments = data_path
    expected = Path(directory).resolve() if arguments else directory

    original_stat = Path.stat

    def unavailable(path, **kwargs):
        if path == directory:
            raise OSError(failure, "synthetic stat failure")
        return original_stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", unavailable)
    assert data_directory(arguments[1] if arguments else None) == expected
