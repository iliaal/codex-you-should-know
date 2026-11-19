"""Project mutes remain effective when a hook omits its optional cwd."""

import io
import json
import time

import pytest

from you_should_know import cli
from you_should_know.assessment import Finding
from you_should_know.state import State


@pytest.mark.parametrize("cwd_mode", ["omitted", "empty", "null", "same", "different"])
@pytest.mark.parametrize("command", ["notify", "reply"])
def test_project_mute_uses_latest_recorded_directory(
    tmp_path, monkeypatch, capsys, cwd_mode, command
):
    directory = tmp_path / "state"
    project = tmp_path / "project"
    other = tmp_path / "other"
    state = State(directory)
    try:
        state.hook_seen("s", "Stop", str(project))
        identifier = state.save(
            "s",
            Finding("lost settings", "Settings deleted.", "Restore them.", ["Deleted."]),
            time.time(),
            notify=True,
        )
        assert identifier is not None
        if command == "reply":
            state.deliver("s", ready=True, turn_id="finished")
        state.mute("another-session", str(project), project=True)
        event = {
            "session_id": "s",
            "hook_event_name": "PostToolUse" if command == "notify" else "UserPromptSubmit",
            "turn_id": "next",
            "prompt": "ysk 3",
        }
        if cwd_mode != "omitted":
            event["cwd"] = {
                "empty": "",
                "null": None,
                "same": str(project),
                "different": str(other),
            }[cwd_mode]
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
        monkeypatch.setattr(cli, "noninteractive_host", lambda: False)
        assert (
            cli.main(["--data-dir", str(directory), "notify" if command == "notify" else "hook"])
            == 0
        )
        output = json.loads(capsys.readouterr().out)
        if cwd_mode == "different":
            assert "systemMessage" in output
            if command == "reply":
                assert state.note(identifier)["status"] == "dismissed"
        else:
            assert output == {}
            assert state.note(identifier)["status"] == "new"
            if command == "notify":
                assert state.has_notifications("s")
    finally:
        state.close()


def test_unknown_session_does_not_inherit_another_project_mute(tmp_path):
    state = State(tmp_path / "state")
    try:
        state.observe("known", False, cwd=str(tmp_path))
        state.mute("known", str(tmp_path), project=True)
        assert state.muted("known")
        assert not state.muted("unknown")
    finally:
        state.close()
