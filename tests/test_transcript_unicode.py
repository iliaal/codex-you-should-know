"""Synthetic escaped Unicode records; no captured conversations or reviewer calls."""

import json
from dataclasses import replace

import pytest
from review_responses import clear_response

from you_should_know import backend, cli
from you_should_know.config import Config
from you_should_know.state import State
from you_should_know.transcript import (
    TranscriptError,
    last_assistant_message,
    read_since,
    read_since_snapshot,
    read_snapshot,
    read_transcript,
    source_blocks,
)


def rollout(path, payload):
    rows = [
        {"type": "session_meta", "payload": {}},
        {"type": "response_item", "payload": payload},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def visible_payload(kind, text):
    if kind in {"user", "assistant"}:
        content = text if kind == "user" else [{"type": "output_text", "text": text}]
        return {"type": "message", "role": kind, "content": content}
    if kind.endswith("_output"):
        return {"type": kind, "call_id": "c", "output": text}
    field = "arguments" if kind == "function_call" else "input"
    return {"type": kind, "name": "example", "call_id": "c", field: text}


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
@pytest.mark.parametrize(
    "kind",
    [
        "user",
        "assistant",
        "function_call",
        "custom_tool_call",
        "function_call_output",
        "custom_tool_call_output",
        "tool_name",
        "call_id",
    ],
)
@pytest.mark.parametrize(
    "reader",
    [read_transcript, read_snapshot, read_since, read_since_snapshot, last_assistant_message],
)
def test_visible_surrogates_report_transcript_location(tmp_path, surrogate, kind, reader):
    path = tmp_path / "synthetic.jsonl"
    text = f"invalid {surrogate} text"
    payload = visible_payload("function_call" if kind in {"tool_name", "call_id"} else kind, text)
    if kind in {"tool_name", "call_id"}:
        payload["arguments"] = "valid input"
        payload["name" if kind == "tool_name" else "call_id"] = text
    rollout(path, payload)
    with pytest.raises(TranscriptError, match="invalid Unicode") as caught:
        if reader in {read_since, read_since_snapshot}:
            reader(path, 0)
        else:
            reader(path)
    assert isinstance(caught.value.__cause__, UnicodeEncodeError)
    assert caught.value.diagnostics["path"] == str(path)
    assert caught.value.diagnostics["line_number"] == 2
    assert caught.value.diagnostics["byte_offset"] == path.read_bytes().index(b"\n") + 1
    assert caught.value.diagnostics["line_prefix"] == repr(
        path.read_bytes().splitlines(keepends=True)[1][:200]
    )


@pytest.mark.parametrize("text", ["valid é e\u0301 🙂 \ud7ff\ue000", r"literal \ud800 escape"])
def test_valid_unicode_and_literal_escape_preserve_exact_text(tmp_path, text):
    path = tmp_path / "synthetic.jsonl"
    rollout(path, visible_payload("assistant", text))
    assert list(source_blocks(read_transcript(path))) == [("assistant", text)]
    assert last_assistant_message(path) == text
    assert list(source_blocks(read_since_snapshot(path, 0)[0])) == [("assistant", text)]


def test_ignored_nonvisible_surrogates_remain_ignored(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    rollout(path, {**visible_payload("assistant", "\ud800"), "channel": "analysis"})
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "session_meta", "payload": {"unused": "\udfff"}}) + "\n")
    assert read_transcript(path) == ""
    assert last_assistant_message(path) == ""


def test_incremental_surrogate_error_keeps_physical_line_number(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    rollout(path, visible_payload("assistant", "Valid prefix"))
    checkpoint = {}
    _, offset = read_snapshot(path, checkpoint=checkpoint)
    record = json.dumps({"type": "response_item", "payload": visible_payload("user", "\ud800")})
    with path.open("a", encoding="utf-8") as stream:
        stream.write(record + "\n")
    with pytest.raises(TranscriptError, match="invalid Unicode") as caught:
        read_since_snapshot(path, offset, expected_checkpoint=checkpoint)
    assert caught.value.diagnostics["line_number"] == 3
    assert caught.value.diagnostics["byte_offset"] == offset


@pytest.mark.parametrize("force", [False, True])
def test_surrogate_transcript_uses_normal_failure_and_recovery(tmp_path, monkeypatch, force):
    path = tmp_path / "synthetic.jsonl"
    rollout(path, visible_payload("user", "invalid \ud800 text"))
    calls = []
    monkeypatch.setattr(backend, "review", lambda *a, **k: calls.append(a) or clear_response())
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    config = replace(Config(), every_n_tools=1)
    state = State(tmp_path / "state")
    try:
        result = cli.check(state, config, "s", path, "PostToolUse", force=force)
        assert result["status"] == ("error" if force else "transcript_unavailable")
        assert "invalid Unicode" in result["reason"]
        assert state.session("s").attempts == 0
        assert state.session("s").consecutive_transcript_failures == 1
        assert calls == []
        errors = state.diagnostics()["recent_transcript_errors"][0]["diagnostics"][
            "transcript_errors"
        ]
        assert len(errors) == 2
        assert all(error["line_number"] == 2 for error in errors)
        if not force:
            assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "skipped"
        rollout(path, visible_payload("user", "Recovered valid conversation"))
        assert cli.check(state, config, "s", path, "Stop", force=True)["status"] == "clear"
        assert len(calls) == 1
        assert state.session("s").consecutive_transcript_failures == 0
    finally:
        state.close()
