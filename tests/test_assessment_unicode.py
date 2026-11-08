"""Reviewer JSON escapes must remain safe to persist without altering valid text."""

import json

import pytest
from review_responses import clear_response

from you_should_know import assessment, backend, cli
from you_should_know.config import Config
from you_should_know.state import State
from you_should_know.transcript import frame


def finding_response(text="Deleted settings.json."):
    return {
        "finding": {
            "topic": text,
            "summary": text,
            "explanation": text,
            "evidence": [text],
            "agent_still_working": False,
            "assistant_claim": False,
        },
        "no_finding": None,
    }


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
@pytest.mark.parametrize("field", ["topic", "summary", "explanation", "evidence"])
def test_finding_rejects_surrogate_text(field, surrogate):
    text = f"invalid {surrogate} text"
    response = finding_response()
    response["finding"][field] = [text] if field == "evidence" else text
    # Python's real JSON decoder accepts the escape, even though UTF-8 cannot encode it.
    response = json.loads(json.dumps(response))
    transcript = frame(
        "tool result: id=1", text if field == "evidence" else "Deleted settings.json."
    )
    with pytest.raises(assessment.AssessmentError, match=f"invalid Unicode in {field}"):
        assessment.parse_finding(response, transcript)


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_clear_result_rejects_surrogate_reason_without_recording_diagnostics(surrogate):
    diagnostics = {}
    response = json.loads(json.dumps(clear_response(reason=f"invalid {surrogate} reason")))
    with pytest.raises(assessment.AssessmentError, match="invalid Unicode in no_finding reason"):
        assessment.parse_finding(response, "conversation", diagnostics=diagnostics)
    assert diagnostics == {}


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_confirmation_rejects_surrogate_reason(confirmed, surrogate):
    response = json.loads(
        json.dumps(
            {
                "confirmed": confirmed,
                "reason": f"invalid {surrogate} reason",
                "evidence": ["Deleted settings.json."],
            }
        )
    )
    with pytest.raises(assessment.AssessmentError, match="invalid Unicode in reason"):
        assessment.parse_confirmation(
            response, frame("tool result: id=1", "Deleted settings.json.")
        )


@pytest.mark.parametrize(
    "text", ["café", "cafe\u0301", "漢字", "\U0001f680", "\ud7ff", "\ue000", "\U0010ffff"]
)
def test_valid_unicode_round_trips_without_normalization(text):
    response = json.loads(json.dumps(finding_response(text)))
    transcript = frame("tool result: id=1", text)
    finding = assessment.parse_finding(response, transcript)
    assert finding == assessment.Finding(text, text, text, [text])
    diagnostics = {}
    assert (
        assessment.parse_finding(clear_response(reason=text), transcript, diagnostics=diagnostics)
        is None
    )
    assert diagnostics["no_finding"]["reason"] == text
    for confirmed in (False, True):
        confirmation = json.loads(
            json.dumps({"confirmed": confirmed, "reason": text, "evidence": [text]})
        )
        assert assessment.parse_confirmation(confirmation, transcript) == (confirmed, text)


@pytest.mark.parametrize("kind", ["finding", "suppressed", "clear", "confirmed", "withdrawn"])
def test_invalid_reviewer_text_records_error_and_allows_retry(tmp_path, monkeypatch, kind):
    path = tmp_path / "synthetic.jsonl"
    rows: list[dict] = [
        {"type": "session_meta", "payload": {}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c",
                "output": "Deleted settings.json.",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    confirming = kind in {"confirmed", "withdrawn"}
    invalid = "invalid \ud800 text"
    if confirming:
        response = {
            "confirmed": kind == "confirmed",
            "reason": invalid,
            "evidence": ["Deleted settings.json."],
        }
    elif kind == "clear":
        response = clear_response(reason=invalid)
    else:
        response = finding_response()
        response["finding"]["summary"] = invalid
        response["finding"]["agent_still_working"] = kind == "suppressed"
    response = json.loads(json.dumps(response))
    responses = ([finding_response()] if confirming else []) + [response, clear_response()]
    calls = []
    usage = {"input_tokens": 123, "output_tokens": 45}

    def review(*args, **kwargs):
        calls.append(args[1])
        return backend.ReviewResponse(responses.pop(0), usage)

    monkeypatch.setattr(backend, "review", review)
    config = Config(every_n_tools=1, min_interval_seconds=0)
    state = State(tmp_path / "state")
    try:
        if confirming:
            assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "pending"
        previous_hash = state.session("s").last_hash if confirming else ""
        result = cli.check(state, config, "s", path, "PostToolUse")
        assert result["status"] == "error"
        assert "invalid Unicode" in result["reason"]
        assert result["usage"] == usage
        assert state.session("s").last_hash == previous_hash
        assert "invalid Unicode" in state.session("s").last_error
        assert state.pending_candidate("s") is None
        assert state.notes(all_notes=True) == []
        candidates = state.candidates(all_candidates=True)
        assert len(candidates) == int(confirming)
        if confirming:
            assert candidates[0]["status"] == "withdrawn"
        recorded = state.diagnostics()["recent_checks"][0]
        assert recorded["outcome"] == "error"
        assert recorded["phase"] == ("confirmation" if confirming else "review")
        rejected = recorded["diagnostics"]["rejected_response"]
        assert not rejected["truncated"]
        assert json.loads(rejected["json"]) == response
        assert recorded["input_tokens"] == 123
        assert recorded["output_tokens"] == 45
        if confirming:
            # Ordinary invalid-confirmation behavior: withdraw, then wait for new work.
            assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "unchanged"
            assert len(calls) == 2
            rows[-1]["payload"]["output"] = "New work completed."
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(rows[-1]) + "\n")
        # A rejected initial result must not mark this transcript successfully reviewed.
        assert cli.check(state, config, "s", path, "PostToolUse")["status"] == "clear"
        assert len(calls) == 2 + int(confirming)
        assert state.session("s").attempts == len(calls)
    finally:
        state.close()
