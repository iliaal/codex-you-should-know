import hashlib
import json
import sys
from contextlib import contextmanager
from dataclasses import replace

import pytest
from review_responses import clear_response

from you_should_know import backend, cli
from you_should_know import transcript as transcript_module
from you_should_know.config import Config
from you_should_know.state import State
from you_should_know.transcript import read_since_snapshot, read_snapshot


def rollout(path, output):
    rows = [
        {"type": "session_meta", "payload": {}},
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "c", "output": output},
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.fixture
def state(tmp_path):
    store = State(tmp_path / "state")
    yield store
    store.close()


@pytest.fixture
def reviewer(monkeypatch):
    calls = []

    def review(prompt, schema, **kwargs):
        calls.append(schema.name)
        quote = "New failure" if "New failure" in prompt else "Old failure"
        if schema == cli.CONFIRMATION_SCHEMA:
            return {"confirmed": True, "reason": "Unresolved", "evidence": [quote]}
        return {
            "no_finding": None,
            "finding": {
                "topic": quote,
                "summary": quote,
                "explanation": quote,
                "evidence": [quote],
                "agent_still_working": False,
                "assistant_claim": False,
            },
        }

    monkeypatch.setattr(backend, "review", review)
    return calls


@pytest.mark.parametrize("change", ["same_size", "grow", "shrink", "different_path"])
def test_changed_transcript_restarts_review_and_can_publish_current_finding(
    state, tmp_path, reviewer, change
):
    path = tmp_path / "rollout.jsonl"
    rollout(path, "Old failure" + (" extra" if change == "shrink" else ""))
    config = replace(Config(), every_n_tools=1)
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
    original = state.pending_candidate("s")
    assert original is not None
    if change == "different_path":
        path = tmp_path / "replacement.jsonl"
    rollout(path, "New failure" + (" extra" if change == "grow" else ""))

    result = cli.check(state, config, "s", path, "Stop")

    assert result["status"] == "finding"
    assert result["note"]["topic"] == "New failure"
    assert reviewer == ["finding.schema.json", "finding.schema.json", "confirmation.schema.json"]
    row = state.connection.execute(
        "SELECT status, reason FROM candidates WHERE id = ?", (original["id"],)
    ).fetchone()
    assert row["status"] == "withdrawn"
    assert "Transcript" in row["reason"]
    assert state.pending_candidate("s") is None


def test_unchanged_prefix_can_confirm_after_append(state, tmp_path, reviewer):
    path = tmp_path / "rollout.jsonl"
    rollout(path, "Old failure")
    config = replace(Config(), every_n_tools=1)
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "event_msg", "payload": {}}) + "\n")

    result = cli.check(state, config, "s", path, "Stop")

    assert result["status"] == "finding"
    assert result["note"]["topic"] == "Old failure"
    assert reviewer == ["finding.schema.json", "confirmation.schema.json"]


def test_background_read_failures_back_off_without_spending_model_budget(
    state, tmp_path, monkeypatch
):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"\x00\n")
    now = [1000.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(backend, "review", lambda *a, **k: clear_response())
    config = replace(Config(), every_n_tools=1)
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "transcript_unavailable"
    for _ in range(4):
        now[0] += 5
        assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "skipped"
    assert state.session("s").consecutive_transcript_failures == 1
    assert state.session("s").attempts == 0
    rollout(path, "Recovered")
    now[0] = 1120.0
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "clear"
    assert state.session("s").consecutive_transcript_failures == 0
    assert state.session("s").attempts == 1


def test_legacy_candidate_is_reviewed_again_after_restart(state, tmp_path, reviewer):
    path = tmp_path / "rollout.jsonl"
    rollout(path, "Old failure")
    config = replace(Config(), every_n_tools=1)
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
    state.connection.execute("UPDATE candidates SET checkpoint = '{}' WHERE session_id = 's'")
    state.connection.commit()
    reopened = State(state.directory)
    try:
        result = cli.check(reopened, config, "s", path, "Stop")
        assert result["status"] == "finding"
        assert reviewer == [
            "finding.schema.json",
            "finding.schema.json",
            "confirmation.schema.json",
        ]
        assert reopened.pending_candidate("s") is None
    finally:
        reopened.close()


def test_changed_prefix_during_confirmation_cannot_publish_stale_note(
    state, tmp_path, reviewer, monkeypatch
):
    path = tmp_path / "rollout.jsonl"
    rollout(path, "Old failure")
    config = replace(Config(), every_n_tools=1)
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
    original_review = backend.review

    def review_and_change(prompt, schema, **kwargs):
        result = original_review(prompt, schema, **kwargs)
        rollout(path, "New failure")
        return result

    monkeypatch.setattr(backend, "review", review_and_change)
    result = cli.check(state, config, "s", path, "Stop")
    assert result["status"] == "withdrawn"
    assert state.notes() == []
    assert state.pending_candidate("s") is None
    monkeypatch.setattr(backend, "review", original_review)
    result = cli.check(state, config, "s", path, "Stop")
    assert result["status"] == "finding"
    assert result["note"]["topic"] == "New failure"


@pytest.mark.parametrize("length", [30, 1_048_600])
def test_checkpoint_hash_covers_only_complete_records(tmp_path, length):
    path = tmp_path / "rollout.jsonl"
    rollout(path, "Old failure")
    prefix = path.read_bytes()
    pending = json.dumps({"type": "event_msg", "payload": {"value": "漢" * length}}).encode()
    with path.open("ab") as stream:
        stream.write(pending)
    checkpoint = {}
    _, offset = read_snapshot(path, checkpoint=checkpoint)
    assert offset == len(prefix)
    assert checkpoint["sha256"] == hashlib.sha256(prefix).hexdigest()
    with path.open("ab") as stream:
        stream.write(b"\n")
    next_checkpoint = {}
    _, next_offset = read_since_snapshot(
        path, offset, expected_checkpoint=checkpoint, checkpoint=next_checkpoint
    )
    assert next_offset == path.stat().st_size
    assert next_checkpoint["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_failure_backoff_survives_restart_and_stop_can_recover(state, tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"\x00\n")
    monkeypatch.setattr(cli.time, "time", lambda: 1000.0)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    monkeypatch.setattr(backend, "review", lambda *a, **k: clear_response())
    config = replace(Config(), every_n_tools=1)
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "transcript_unavailable"
    reopened = State(state.directory)
    try:
        rollout(path, "Recovered")
        assert cli.check(reopened, config, "s", path, "PostToolUse")["status"] == "skipped"
        assert cli.check(reopened, config, "s", path, "Stop")["status"] == "clear"
        assert reopened.session("s").last_transcript_failure == 0
        assert reopened.session("s").attempts == 1
    finally:
        reopened.close()


def test_legacy_candidate_restart_survives_exhausted_budget(state, tmp_path, reviewer, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    rollout(path, "Old failure")
    now = [1000.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    config = replace(Config(), every_n_tools=1, max_checks_per_hour=1)
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
    state.connection.execute("UPDATE candidates SET checkpoint = '{}' WHERE session_id = 's'")
    state.connection.commit()
    result = cli.check(state, config, "s", path, "Stop")
    assert result["status"] == "skipped"
    assert result["reason"] == "budget exhausted"
    now[0] = 5000.0
    assert cli.check(state, config, "s", path, "Stop")["status"] == "pending"
    now[0] = 9000.0
    result = cli.check(state, config, "s", path, "Stop")
    assert result["status"] == "finding"
    assert result["note"]["topic"] == "Old failure"
    assert reviewer == ["finding.schema.json", "finding.schema.json", "confirmation.schema.json"]


@pytest.mark.parametrize("change", ["replace", "rewrite", "append"])
def test_final_checkpoint_read_cannot_publish_after_mid_read_change(
    state, tmp_path, reviewer, monkeypatch, change
):
    path = tmp_path / "rollout.jsonl"
    rollout(path, "Old failure")
    config = replace(Config(), every_n_tools=1)
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
    original_stream = transcript_module._stream
    opens = 0

    class ChangingReader:
        def __init__(self, stream):
            self.stream = stream
            self.changed = False

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size):
            data = self.stream.read(size)
            if data and not self.changed:
                self.changed = True
                if change == "replace":
                    replacement = tmp_path / "replacement.jsonl"
                    rollout(replacement, "New failure")
                    replacement.replace(path)
                elif change == "rewrite":
                    rollout(path, "New failure")
                else:
                    row = {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "new",
                            "output": "New failure",
                        },
                    }
                    with path.open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
            return data

    @contextmanager
    def changing_stream(*args):
        nonlocal opens
        opens += 1
        with original_stream(*args) as stream:
            yield ChangingReader(stream) if opens == 3 else stream

    monkeypatch.setattr(transcript_module, "_stream", changing_stream)
    result = cli.check(state, config, "s", path, "Stop")
    assert result["status"] == "withdrawn"
    assert state.notes() == []
    assert state.pending_candidate("s") is None
    assert "New failure" in read_snapshot(path)[0]
    result = cli.check(state, config, "s", path, "Stop")
    assert result["status"] == "finding"
    assert result["note"]["topic"] == "New failure"


def test_final_read_retries_visible_append_after_eof(state, tmp_path, reviewer, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    rollout(path, "Old failure")
    config = replace(Config(), every_n_tools=1)
    assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
    original_stream = transcript_module._stream
    opens = 0

    class AppendingReader:
        def __init__(self, stream):
            self.stream = stream
            self.changed = False

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def readline(self, size):
            data = self.stream.readline(size)
            if not data and not self.changed:
                self.changed = True
                row = {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "The failure is fixed now",
                    },
                }
                with path.open("a") as stream:
                    stream.write(json.dumps(row) + "\n")
            return data

    @contextmanager
    def appending_stream(*args):
        nonlocal opens
        opens += 1
        with original_stream(*args) as stream:
            yield AppendingReader(stream) if opens == 3 else stream

    monkeypatch.setattr(transcript_module, "_stream", appending_stream)
    result = cli.check(state, config, "s", path, "Stop")
    assert result["status"] == "withdrawn"
    assert result["reason"] == "Conversation advanced during confirmation"
    assert state.notes() == []
    assert "The failure is fixed now" in read_snapshot(path)[0]


def test_integer_decode_failure_uses_background_transcript_recovery(state, tmp_path, reviewer):
    path = tmp_path / "rollout.jsonl"
    rollout(path, "Old failure")
    limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        with path.open("a") as stream:
            stream.write('{"type":"token_usage_record","payload":{"tokens":' + "9" * 641 + "}}\n")
        result = cli.check(state, replace(Config(), every_n_tools=1), "s", path, "PostToolUse")
        assert result["status"] == "transcript_unavailable"
        assert state.session("s").consecutive_transcript_failures == 1
        assert state.session("s").attempts == 0
        assert reviewer == []
        errors = state.diagnostics()["recent_transcript_errors"][0]["diagnostics"][
            "transcript_errors"
        ]
        assert len(errors) == 2  # The normal bounded read retry also ran.
        assert all(error["line_number"] == 3 for error in errors)
        assert all(error["reason"] == "Invalid JSONL at transcript line 3" for error in errors)
    finally:
        sys.set_int_max_str_digits(limit)
