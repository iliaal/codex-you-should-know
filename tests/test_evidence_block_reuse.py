import pytest

from you_should_know import assessment
from you_should_know.transcript import frame


def parse(kind, quotes, transcript, *, assistant_claim=False):
    if kind == "finding":
        return assessment.parse_finding(
            {
                "finding": {
                    "topic": "settings overwritten",
                    "summary": "Your previous settings were removed.",
                    "explanation": "The tool result contradicts the saved settings claim.",
                    "evidence": quotes,
                    "agent_still_working": False,
                    "assistant_claim": assistant_claim,
                },
                "no_finding": None,
            },
            transcript,
        )
    return assessment.parse_confirmation(
        {"confirmed": kind == "confirmed", "reason": "Review result", "evidence": quotes},
        transcript,
        assistant_claim=assistant_claim,
    )


@pytest.mark.parametrize("kind", ["finding", "confirmed", "withdrawn"])
@pytest.mark.parametrize("count", [1, 2, 3])
def test_one_source_parse_per_decision(monkeypatch, kind, count):
    quotes = ["preferences lost", "settings preserved", "keep my settings"][:count]
    transcript = "\n\n".join(
        frame(label, text)
        for label, text in zip(
            ["tool result: id=1", "assistant final", "user"], quotes, strict=False
        )
    )
    original = assessment._evidence_blocks
    calls = []

    def tracked(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(assessment, "_evidence_blocks", tracked)
    result = parse(kind, quotes, transcript, assistant_claim=count > 1)
    if kind == "finding":
        assert result == assessment.Finding(
            "settings overwritten",
            "Your previous settings were removed.",
            "The tool result contradicts the saved settings claim.",
            quotes,
        )
    else:
        assert result == (kind == "confirmed", "Review result")
    assert calls == [transcript]


@pytest.mark.parametrize("kind", ["finding", "confirmed", "withdrawn"])
def test_source_reuse_is_limited_to_one_decision(kind):
    quote = "preferences lost"
    valid = frame("tool result: id=1", quote)
    assert parse(kind, [quote], valid)
    # The same quote and apparent tool label now belong to user text.
    spoofed = frame("user", valid)
    if kind == "withdrawn":
        assert parse(kind, [quote], spoofed) == (False, "Review result")
    else:
        with pytest.raises(assessment.AssessmentError, match="tool-result block"):
            parse(kind, [quote], spoofed)
    assert parse(kind, [quote], valid)


@pytest.mark.parametrize("kind", ["finding", "confirmed", "withdrawn"])
@pytest.mark.parametrize("quotes", [[42], ["not in the conversation"]])
def test_invalid_or_missing_first_quote_does_not_parse_sources(monkeypatch, kind, quotes):
    def unexpected_parse(text):
        pytest.fail("Invalid or absent evidence should fail before source parsing")

    monkeypatch.setattr(assessment, "_evidence_blocks", unexpected_parse)
    with pytest.raises(assessment.AssessmentError):
        parse(kind, quotes, frame("tool result: id=1", "preferences lost"))
