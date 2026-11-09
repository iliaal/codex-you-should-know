"""Synthetic reviewer output only; no native Codex installation or model calls."""

import json
import sys
from pathlib import Path

import pytest

from you_should_know import backend, cli
from you_should_know.config import Config
from you_should_know.state import State


def nested_json(kind: str, depth: int) -> str:
    opening, closing = ("[", "]") if kind == "array" else ('{"":', "}")
    return opening * depth + '"private-recursion-canary"' + closing * depth


@pytest.fixture
def fake_reviewer(tmp_path, monkeypatch):
    executable = tmp_path / "codex"
    monkeypatch.setattr(backend.shutil, "which", lambda name: str(executable))
    schema = tmp_path / "schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")

    def configure(raw: str, source: str, ending: str = "\n") -> Path:
        event = '{"type":"thread.started","extra":' + raw + "}"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "output = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
            f"output.write_text({raw if source == 'result' else '{}'!r}, encoding='utf-8')\n"
            'print(\'{"type":"turn.completed","usage":{'
            '"input_tokens":123,"output_tokens":45}}\', flush=True)\n'
            + (f"sys.stdout.write({event + ending!r})\n" if source == "stream" else ""),
            encoding="utf-8",
        )
        executable.chmod(0o700)
        return schema

    return configure


@pytest.mark.parametrize("kind", ["array", "object"])
@pytest.mark.parametrize("source,ending", [("stream", "\n"), ("stream", ""), ("result", "")])
def test_excessively_nested_reviewer_json_is_a_backend_error(fake_reviewer, kind, source, ending):
    raw = '{"finding":' + nested_json(kind, 10_000) + "}"
    assert len(raw.encode()) < backend.MAX_RESULT_BYTES < backend.MAX_OUTPUT_BYTES
    schema = fake_reviewer(raw, source, ending)
    with pytest.raises(backend.BackendError) as caught:
        backend.review("synthetic test", schema, timeout=5)
    error = caught.value
    assert isinstance(error.__cause__, RecursionError)
    assert error.usage == {"input_tokens": 123, "output_tokens": 45}
    assert error.process_started
    assert error.diagnostics == {
        "category": "process" if source == "stream" else "result",
        "stage": "turn_completed" if source == "stream" else "result",
        "timeout_seconds": 5,
    }
    assert str(error) == (
        "Codex returned an invalid event stream"
        if source == "stream"
        else "Codex returned no valid JSON review"
    )
    assert "private-recursion-canary" not in str(error) + json.dumps(error.diagnostics)


@pytest.mark.parametrize("kind", ["array", "object"])
@pytest.mark.parametrize("source,ending", [("stream", "\n"), ("stream", ""), ("result", "")])
def test_shallow_reviewer_json_remains_unchanged(fake_reviewer, kind, source, ending):
    raw = '{"finding":' + nested_json(kind, 3) + "}"
    schema = fake_reviewer(raw, source, ending)
    result = backend.review("synthetic test", schema, timeout=5)
    assert result == (json.loads(raw) if source == "result" else {})
    assert result.usage == {"input_tokens": 123, "output_tokens": 45}


@pytest.mark.parametrize("source", ["stream", "result"])
@pytest.mark.parametrize("confirming", [False, True])
def test_nested_review_failure_is_recorded_and_can_recover(
    fake_reviewer, tmp_path, source, confirming
):
    path = tmp_path / "synthetic.jsonl"
    row = {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "c", "output": "Lost settings."},
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    config = Config(every_n_tools=1, min_interval_seconds=0)
    state = State(tmp_path / "state")
    try:
        if confirming:
            fake_reviewer(
                json.dumps(
                    {
                        "finding": {
                            "topic": "settings",
                            "summary": "Settings were lost.",
                            "explanation": "Settings were lost.",
                            "evidence": ["Lost settings."],
                            "agent_still_working": False,
                            "assistant_claim": False,
                        },
                        "no_finding": None,
                    }
                ),
                "result",
            )
            assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
        previous_hash = state.session("s").last_hash if confirming else ""
        raw = '{"finding":' + nested_json("array", 10_000) + "}"
        fake_reviewer(raw, source)
        result = cli.check(state, config, "s", path, "PostToolUse")
        assert result["status"] == "error"
        assert result["usage"] == {"input_tokens": 123, "output_tokens": 45}
        assert state.session("s").last_error == result["reason"]
        assert state.session("s").last_hash == previous_hash
        assert state.session("s").attempts == 1 + int(confirming)
        assert state.notes(all_notes=True) == []
        assert state.pending_candidate("s") is None
        candidates = state.candidates(all_candidates=True)
        assert len(candidates) == int(confirming)
        if confirming:
            assert candidates[0]["status"] == "withdrawn"
        recorded = state.diagnostics()["recent_checks"][0]
        assert recorded["outcome"] == "error"
        assert recorded["phase"] == ("confirmation" if confirming else "review")
        assert recorded["input_tokens"] == 123
        assert recorded["output_tokens"] == 45
        assert recorded["diagnostics"]["backend"]["category"] == (
            "process" if source == "stream" else "result"
        )
        assert "private-recursion-canary" not in json.dumps(recorded)
        fake_reviewer(
            json.dumps(
                {
                    "finding": None,
                    "no_finding": {"category": "no_concern", "reason": "Nothing to report."},
                }
            ),
            "result",
        )
        if confirming:
            assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "unchanged"
            row["payload"]["output"] = "New work completed."
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
        assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "clear"
        assert state.session("s").attempts == 2 + int(confirming)
    finally:
        state.close()
