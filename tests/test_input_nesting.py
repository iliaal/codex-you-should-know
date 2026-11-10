"""Synthetic host input only; no captured sessions or reviewer model calls."""

import io
import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest
from review_responses import clear_response

from you_should_know import backend, cli, transcript
from you_should_know.config import Config
from you_should_know.state import State

READERS = ["full", "snapshot", "since", "since_snapshot", "latest"]


def nested_json(kind, depth=10_000):
    opening, closing = ("[", "]") if kind == "array" else ('{"nested":', "}")
    return opening * depth + '"synthetic-private-canary"' + closing * depth


def metadata(kind, depth=10_000):
    return '{"type":"token_usage_record","payload":{"extra":' + nested_json(kind, depth) + "}}"


def rollout(tmp_path):
    path = tmp_path / "synthetic.jsonl"
    rows = [
        {"type": "session_meta", "payload": {}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "Synthetic failure",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + "\n", encoding="utf-8")
    return path


def read(path, reader, offset):
    if reader == "full":
        return transcript.read_transcript(path)
    if reader == "snapshot":
        return transcript.read_snapshot(path)[0]
    if reader == "since":
        return transcript.read_since(path, offset)
    if reader == "since_snapshot":
        return transcript.read_since_snapshot(path, offset)[0]
    return transcript.last_assistant_message(path)


@pytest.mark.parametrize("kind", ["array", "object"])
@pytest.mark.parametrize("reader", READERS)
def test_nested_transcript_json_reports_physical_location(tmp_path, kind, reader):
    path = rollout(tmp_path)
    offset = path.stat().st_size
    raw = (metadata(kind) + "\n").encode()
    assert len(raw) < transcript._MAX_LINE_BYTES
    with path.open("ab") as stream:
        stream.write(raw)
    with pytest.raises(
        transcript.TranscriptError, match="Invalid JSONL at transcript line 4"
    ) as caught:
        read(path, reader, offset)
    assert isinstance(caught.value.__cause__, RecursionError)
    assert caught.value.diagnostics == {
        "path": str(path),
        "file_size": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
        "line_number": 4,
        "byte_offset": offset,
        "line_prefix": repr(raw[:200]),
    }
    assert "synthetic-private-canary" not in str(caught.value)


@pytest.mark.parametrize("kind", ["array", "object"])
@pytest.mark.parametrize("reader", READERS)
def test_shallow_transcript_metadata_remains_supported(tmp_path, kind, reader):
    path = rollout(tmp_path)
    offset = path.stat().st_size
    expected = read(path, reader, offset)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(metadata(kind, 3) + "\n")
    assert read(path, reader, offset) == expected


@pytest.mark.parametrize("kind", ["array", "object"])
@pytest.mark.parametrize("reader", READERS)
def test_unterminated_nested_record_retains_incomplete_tail_policy(tmp_path, kind, reader):
    path = rollout(tmp_path)
    offset = path.stat().st_size
    expected = read(path, reader, offset)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(metadata(kind))
    result = read(path, reader, offset)
    if reader == "latest":
        assert result == expected
    else:
        assert result == (expected + "\n\n" if expected else "") + transcript._INCOMPLETE
    checkpoint = {}
    _, cursor = transcript.read_snapshot(path, checkpoint=checkpoint)
    assert cursor == offset
    path.write_bytes(path.read_bytes()[:offset])
    expected_checkpoint = {}
    transcript.read_snapshot(path, checkpoint=expected_checkpoint)
    assert checkpoint == expected_checkpoint


@pytest.mark.parametrize("kind", ["array", "object"])
@pytest.mark.parametrize("reader", READERS)
def test_oversized_nested_record_is_omitted_before_decoding(tmp_path, kind, reader):
    path = rollout(tmp_path)
    offset = path.stat().st_size
    expected = read(path, reader, offset)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(metadata(kind) + " " * transcript._MAX_LINE_BYTES + "\n")
    result = read(path, reader, offset)
    if reader == "latest":
        assert result == expected
    else:
        assert (
            result
            == (expected + "\n\n" if expected else "") + "[Oversized transcript record omitted]"
        )


@pytest.mark.parametrize("kind", ["array", "object"])
def test_nested_event_json_has_normal_validation_error(monkeypatch, kind):
    raw = '{"session_id":"s","hook_event_name":"Stop","extra":' + nested_json(kind) + "}"
    assert len(raw) < 2_000_000
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    with pytest.raises(ValueError, match="Hook input exceeds JSON nesting limit") as caught:
        cli.read_event()
    assert isinstance(caught.value.__cause__, RecursionError)


@pytest.mark.parametrize("kind", ["array", "object"])
@pytest.mark.parametrize("command", ["hook", "notify"])
def test_nested_event_stdin_is_advisory_without_state_or_traceback(tmp_path, kind, command):
    directory = tmp_path / "unused-state"
    raw = '{"session_id":"s","hook_event_name":"Stop","extra":' + nested_json(kind) + "}"
    env = os.environ.copy()
    env.pop("YSK_REVIEWER_ACTIVE", None)
    result = subprocess.run(
        [sys.executable, "scripts/you_should_know.py", "--data-dir", str(directory), command],
        input=raw,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "systemMessage": "You should know: Hook input exceeds JSON nesting limit"
    }
    assert not directory.exists()


@pytest.mark.parametrize("kind", ["array", "object"])
def test_shallow_event_extra_fields_remain_unchanged(monkeypatch, kind):
    raw = '{"session_id":"s","hook_event_name":"Stop","extra":' + nested_json(kind, 3) + "}"
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert cli.read_event() == json.loads(raw)


@pytest.mark.parametrize("kind", ["array", "object"])
def test_event_size_guard_precedes_nesting_guard(monkeypatch, kind):
    raw = nested_json(kind) + " " * 2_000_001
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    with pytest.raises(ValueError, match="Hook input exceeds 2 MB") as caught:
        cli.read_event()
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("kind", ["array", "object"])
def test_nested_transcript_retries_then_recovers_without_spending_budget(
    tmp_path, monkeypatch, kind
):
    path = rollout(tmp_path)
    good = path.read_bytes()
    bad = (metadata(kind) + "\n").encode()
    path.write_bytes(good + bad)
    calls = []
    sleeps = []
    now = [1000.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)

    def reviewer(*args, **kwargs):
        calls.append(True)
        return clear_response()

    monkeypatch.setattr(backend, "review", reviewer)
    state = State(tmp_path / "state")
    try:
        config = replace(Config(), every_n_tools=1)
        result = cli.check(state, config, "s", path, "PostToolUse")
        assert result["status"] == "transcript_unavailable"
        assert sleeps == [0.1]
        assert calls == []
        assert state.session("s").attempts == 0
        assert state.session("s").consecutive_transcript_failures == 1
        errors = state.diagnostics()["recent_transcript_errors"][0]["diagnostics"][
            "transcript_errors"
        ]
        assert len(errors) == 2
        for error in errors:
            assert error["reason"] == "Invalid JSONL at transcript line 4"
            assert error["line_number"] == 4
            assert error["byte_offset"] == len(good)
            assert error["line_prefix"] == repr(bad[:200])
        now[0] += 5
        assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "skipped"
        assert calls == []
        path.write_bytes(good)
        now[0] = 1120.0
        assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "clear"
        assert calls == [True]
        assert state.session("s").attempts == 1
        assert state.session("s").consecutive_transcript_failures == 0
    finally:
        state.close()


@pytest.mark.parametrize("kind", ["array", "object"])
def test_nested_transcript_manual_check_returns_recorded_error(tmp_path, monkeypatch, capsys, kind):
    path = rollout(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(metadata(kind) + "\n")
    directory = tmp_path / "state"
    monkeypatch.setattr(backend, "review", lambda *a, **k: pytest.fail("Must not call reviewer"))
    assert (
        cli.main(
            ["--data-dir", str(directory), "check", "--session", "s", "--transcript", str(path)]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["status"] == "error"
    state = State(directory)
    try:
        assert state.session("s").attempts == 0
        assert state.session("s").consecutive_transcript_failures == 1
        assert len(state.diagnostics()["recent_transcript_errors"]) == 1
    finally:
        state.close()


@pytest.mark.parametrize("kind", ["array", "object"])
def test_nested_transcript_reply_requires_explicit_shortcut(tmp_path, kind):
    path = rollout(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(metadata(kind) + "\n")
    errors = []
    assert cli.assistant_waiting(str(path), on_error=errors.append)
    assert len(errors) == 1
    assert errors[0].diagnostics["line_number"] == 4
    assert isinstance(errors[0].__cause__, RecursionError)


@pytest.mark.parametrize("kind", ["array", "object"])
@pytest.mark.parametrize("phase", ["before_confirmation", "after_confirmation"])
def test_nested_transcript_cannot_publish_candidate(tmp_path, monkeypatch, kind, phase):
    path = rollout(tmp_path)
    rows = [
        {"type": "session_meta", "payload": {}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c",
                "output": "Synthetic failure",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows) + "\n", encoding="utf-8")
    good = path.read_bytes()
    bad = (metadata(kind) + "\n").encode()
    calls = []

    def reviewer(prompt, schema, **kwargs):
        calls.append(schema.name)
        if schema == cli.CONFIRMATION_SCHEMA:
            if phase == "after_confirmation":
                path.write_bytes(good + bad)
            return {"confirmed": True, "reason": "Unresolved", "evidence": ["Synthetic failure"]}
        return {
            "no_finding": None,
            "finding": {
                "topic": "Synthetic failure",
                "summary": "Synthetic failure",
                "explanation": "Synthetic failure",
                "evidence": ["Synthetic failure"],
                "agent_still_working": False,
                "assistant_claim": False,
            },
        }

    monkeypatch.setattr(backend, "review", reviewer)
    state = State(tmp_path / "state")
    try:
        config = replace(Config(), every_n_tools=1)
        assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
        candidate = state.pending_candidate("s")
        assert candidate is not None
        if phase == "before_confirmation":
            path.write_bytes(good + bad)
        assert cli.check(state, config, "s", path, "Stop")["status"] == "transcript_unavailable"
        assert state.notes() == []
        row = state.connection.execute(
            "SELECT status FROM candidates WHERE id = ?", (candidate["id"],)
        ).fetchone()
        assert row["status"] == ("pending" if phase == "before_confirmation" else "withdrawn")
        assert state.session("s").attempts == (1 if phase == "before_confirmation" else 2)
        assert calls == (
            ["finding.schema.json"]
            if phase == "before_confirmation"
            else ["finding.schema.json", "confirmation.schema.json"]
        )
        errors = state.diagnostics()["recent_transcript_errors"][0]["diagnostics"][
            "transcript_errors"
        ]
        assert len(errors) == 2
        assert all(error["line_number"] == 4 for error in errors)
    finally:
        state.close()
