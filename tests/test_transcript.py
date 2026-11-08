"""Synthetic Codex rollout fixtures; no captured user conversations."""

import json
import sys
import tracemalloc
from pathlib import Path

import pytest

from you_should_know.transcript import (
    TranscriptError,
    frame,
    last_assistant_message,
    read_since,
    read_snapshot,
    read_transcript,
    source_blocks,
)


def rollout(tmp_path: Path, *payloads: dict) -> Path:
    path = tmp_path / "synthetic-rollout.jsonl"
    rows = [{"type": "session_meta", "payload": {"cli_version": "0.162.1"}}]
    rows.extend({"type": "response_item", "payload": payload} for payload in payloads)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def message(role: str, text: str) -> dict:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def test_extracts_messages_and_both_tool_call_formats(tmp_path: Path) -> None:
    path = rollout(
        tmp_path,
        message("user", "Please inspect the synthetic fixture"),
        message("assistant", "I will inspect it"),
        {"type": "function_call", "name": "inspect", "call_id": "f1", "arguments": '{"a": 1}'},
        {"type": "function_call_output", "call_id": "f1", "output": "first result"},
        {"type": "custom_tool_call", "name": "patch", "call_id": "c2", "input": "patch input"},
        {
            "type": "custom_tool_call_output",
            "call_id": "c2",
            "output": [{"type": "input_text", "text": "second result"}],
        },
    )
    assert read_transcript(path) == (
        "[user; chars=36]\nPlease inspect the synthetic fixture\n\n"
        "[assistant; chars=17]\nI will inspect it\n\n"
        '[tool call: inspect; id=f1; chars=8]\n{"a": 1}\n\n'
        "[tool result: id=f1; chars=12]\nfirst result\n\n"
        "[tool call: patch; id=c2; chars=11]\npatch input\n\n"
        "[tool result: id=c2; chars=13]\nsecond result"
    )


def test_excludes_private_and_nontext_items(tmp_path: Path) -> None:
    forbidden = "FORBIDDEN_SYNTHETIC_REASONING_2d95"
    path = rollout(
        tmp_path,
        {"type": "reasoning", "summary": [{"text": forbidden}], "encrypted_content": forbidden},
        {**message("assistant", forbidden), "channel": "analysis"},
        message("developer", forbidden),
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": forbidden},
                {"type": "encrypted_content", "encrypted_content": forbidden},
                {"type": "input_text", "text": "Visible synthetic question"},
            ],
        },
    )
    assert forbidden not in read_transcript(path)
    assert read_transcript(path) == "[user; chars=26]\nVisible synthetic question"


@pytest.mark.parametrize("reader", [read_transcript, last_assistant_message])
def test_valid_empty_history_is_distinct_from_missing_input(tmp_path: Path, reader) -> None:
    assert reader(rollout(tmp_path)) == ""
    with pytest.raises(TranscriptError, match="Cannot read transcript"):
        reader(tmp_path / "missing.jsonl")


@pytest.mark.parametrize(
    "data",
    [
        "",
        "{}\n",
        "[]\n",
        '{"messages": []}\n',
        '{"type": "session_meta"}\n',
        '{"type": []}\n',
        '{"type": "future_record", "payload": {}}\n',
        '{"type": "response_item", "payload": null}\n',
        '{"type": "response_item", "payload": {}}\n',
    ],
)
@pytest.mark.parametrize("reader", [read_transcript, last_assistant_message])
def test_rejects_unsupported_or_empty_files(tmp_path: Path, data: str, reader) -> None:
    path = tmp_path / "unsupported.jsonl"
    path.write_text(data, encoding="utf-8")
    with pytest.raises(TranscriptError):
        reader(path)


@pytest.mark.parametrize("reader", [read_transcript, last_assistant_message])
def test_malformed_record_after_valid_history_reports_its_location(tmp_path: Path, reader):
    path = rollout(tmp_path, message("assistant", "Done."))
    malformed = '{"type": "response_item", "payload": null}\n'
    with path.open("a", encoding="utf-8") as stream:
        stream.write(malformed)
    with pytest.raises(TranscriptError, match="payload object") as caught:
        reader(path)
    assert caught.value.diagnostics["path"] == str(path)
    assert caught.value.diagnostics["line_number"] == 3
    assert caught.value.diagnostics["line_prefix"] == repr(malformed.encode())


def test_latest_assistant_reader_tolerates_unknown_records_in_valid_history(tmp_path: Path):
    path = rollout(tmp_path, message("assistant", "Done."))
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"type": "future_record", "payload": {}}\n')
    assert last_assistant_message(path) == "Done."


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "message", "role": "user"},
        {"type": "message", "role": "user", "content": None},
        {"type": "message", "role": "user", "content": [{"type": "input_text"}]},
        {"type": "function_call", "name": "inspect"},
        {"type": "function_call_output"},
    ],
)
@pytest.mark.parametrize("reader", [read_transcript, last_assistant_message])
def test_rejects_malformed_visible_records(tmp_path: Path, payload: dict, reader) -> None:
    with pytest.raises(TranscriptError):
        reader(rollout(tmp_path, payload))


def test_retains_initial_intent_and_latest_work_when_bounded(tmp_path: Path) -> None:
    path = rollout(
        tmp_path,
        message("user", "Initial synthetic intent: repair the exporter"),
        *(message("assistant", f"Intermediate work {i}: " + "x" * 80) for i in range(100)),
        message("assistant", "Latest synthetic result: exporter verified"),
    )
    text = read_transcript(path, max_chars=512)
    assert len(text) <= 512
    assert "Initial synthetic intent: repair the exporter" in text
    assert "Latest synthetic result: exporter verified" in text
    assert "earlier conversation omitted" in text
    assert "Intermediate work 0:" not in text


def test_preserves_request_after_initial_user_instruction_messages(tmp_path: Path) -> None:
    path = rollout(
        tmp_path,
        message("user", "Synthetic repository instructions: " + "x" * 4_000),
        message("user", "Synthetic environment context: " + "y" * 4_000),
        message("user", "Actual request: repair exporter"),
        *(message("assistant", "Intermediate synthetic work: " + "z" * 80) for _ in range(100)),
        message("assistant", "Latest synthetic result: exporter verified"),
    )
    text = read_transcript(path, max_chars=512)
    assert len(text) <= 512
    assert sum(label == "user" for label, _ in source_blocks(text)) == 3
    assert "Actual request: repair exporter" in text
    assert "Latest synthetic result: exporter verified" in text


def test_three_initial_messages_fit_the_minimum_budget(tmp_path: Path) -> None:
    path = rollout(
        tmp_path,
        *(message("user", str(i) * 1_000) for i in range(3)),
        message("assistant", "Latest result"),
    )
    text = read_transcript(path, max_chars=256)
    assert len(text) <= 256
    assert "Latest result" in text
    assert sum(label == "user" for label, _ in source_blocks(text)) == 3


@pytest.mark.parametrize("budget", [1000, 60000, 200000])
def test_resumed_session_keeps_latest_requests_after_tool_heavy_work(tmp_path, budget):
    requests = ["Current request: review the exporter", "Keep the existing format", "Continue"]
    path = rollout(
        tmp_path,
        *(message("user", f"Obsolete request {i}") for i in range(3)),
        *(message("user", request) for request in requests),
        *(
            {"type": "function_call_output", "call_id": str(i), "output": "work " * 1000}
            for i in range(60)
        ),
        message("assistant", "Current exporter review complete"),
    )
    text, offset = read_snapshot(path, budget)
    assert len(text) <= budget
    assert "Obsolete request" not in text
    assert all(text.count(request) == 1 for request in requests)
    assert [text.index(request) for request in requests] == sorted(
        text.index(request) for request in requests
    )
    assert "Current exporter review complete" in text
    assert offset == path.stat().st_size


@pytest.mark.parametrize("phase_field", [{"phase": "final_answer"}, {"channel": "final"}])
def test_disclosure_in_recent_activity_survives_user_reservation(tmp_path, phase_field):
    disclosure = "ALREADY DISCLOSED: timestamps use the wrong timezone."
    latest = "Latest result: configuration reviewed."
    path = rollout(
        tmp_path,
        message("user", "CURRENT REQUEST: " + "u" * 500),
        {"type": "function_call_output", "call_id": "old", "output": "old" * 200},
        {**message("assistant", disclosure), **phase_field},
        {"type": "function_call_output", "call_id": "new", "output": "x" * 600},
        {**message("assistant", latest), **phase_field},
    )
    text = read_transcript(path, 1000)
    assert len(text) <= 1000
    assert "CURRENT REQUEST" in text
    assert disclosure in text and latest in text
    assert text.index(disclosure) < text.index(latest)
    assert text.count(disclosure) == 1


def test_disclosures_outside_recent_activity_survive_tool_heavy_followup(tmp_path):
    disclosures = [
        "Disclosed limitation: no timezone conversion.",
        "Disclosed cost: extra storage.",
    ]
    path = rollout(
        tmp_path,
        message("user", "Investigate the exporter"),
        *(
            {**message("assistant", disclosure), "phase": "final_answer"}
            for disclosure in disclosures
        ),
        message("user", "Continue checking configuration"),
        *(
            {"type": "function_call_output", "call_id": str(i), "output": "work " * 1000}
            for i in range(60)
        ),
        message("assistant", "The check is still running"),
    )
    text = read_transcript(path, 2000)
    assert len(text) <= 2000
    assert all(text.count(disclosure) == 1 for disclosure in disclosures)
    assert text.index(disclosures[0]) < text.index(disclosures[1]) < text.index("Continue checking")
    assert "The check is still running" in text


def test_recent_final_reply_uses_room_beyond_its_reserved_minimum(tmp_path):
    latest = "Current final reply: " + "z" * 300
    path = rollout(
        tmp_path,
        message("user", "Current question"),
        {"type": "function_call_output", "call_id": "old", "output": "x" * 2000},
        {**message("assistant", latest), "phase": "final_answer"},
    )
    text = read_transcript(path, 1000)
    assert len(text) <= 1000
    assert latest in text


def test_reclipping_never_leaves_partial_generated_markers_as_evidence():
    from you_should_know.transcript import _TRUNCATED, _clip_block

    block = _clip_block(frame("user", "A" * 1000), 200)
    for budget in range(20, 200):
        clipped = _clip_block(block, budget)
        assert len(clipped) <= budget
        for _, body in source_blocks(clipped):
            assert set(body.replace(_TRUNCATED, "").replace("…", "")) <= {"A"}


def test_marks_truncation_inside_a_large_message(tmp_path: Path) -> None:
    text = read_transcript(rollout(tmp_path, message("user", "START" + "x" * 4_000 + "END")), 256)
    assert len(text) <= 256
    assert "START" in text
    assert "END" in text
    assert "content truncated" in text


def test_tolerates_an_incomplete_final_record(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("user", "Complete synthetic message"))
    with path.open("ab") as stream:
        stream.write(b'{"type":"response_item","payload":')
    text = read_transcript(path)
    assert "Complete synthetic message" in text
    assert "Incomplete final transcript record omitted" in text


def test_accepts_complete_final_record_without_newline(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("user", "No newline required"))
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    assert read_transcript(path) == "[user; chars=19]\nNo newline required"


def test_rejects_malformed_completed_record(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("user", "Complete synthetic message"))
    with path.open("ab") as stream:
        stream.write(b'{"broken":\n')
    with pytest.raises(TranscriptError, match="Invalid JSONL"):
        read_transcript(path)


def test_skips_oversized_record_and_reads_later_work(tmp_path: Path) -> None:
    path = rollout(
        tmp_path,
        message("user", "Initial synthetic request"),
        message("assistant", "x" * 1_100_000),
        message("assistant", "Latest small result"),
    )
    text = read_transcript(path, 512)
    assert "Oversized transcript record omitted" in text
    assert "Latest small result" in text
    assert len(text) <= 512


def test_large_record_does_not_require_loading_the_file_into_memory(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("user", "Initial synthetic request"))
    with path.open("ab") as stream:
        stream.write(b'{"type":"response_item","payload":{"type":"reasoning","encrypted_content":"')
        for _ in range(4_096):
            stream.write(b"x" * 4_096)
        stream.write(b'"}}\n')
        stream.write(
            json.dumps(
                {"type": "response_item", "payload": message("assistant", "Latest result")}
            ).encode()
            + b"\n"
        )
    tracemalloc.start()
    try:
        text = read_transcript(path, 512)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "Latest result" in text
    assert "Oversized transcript record omitted" in text
    assert peak < 8 * 1_024 * 1_024


def test_does_not_duplicate_event_messages(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("assistant", "Unique result"))
    with path.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "Unique result"},
                }
            )
            + "\n"
        )
    assert read_transcript(path).count("Unique result") == 1


@pytest.mark.parametrize("max_chars", [0, 255, True])
def test_rejects_impossibly_small_budgets(tmp_path: Path, max_chars: int) -> None:
    with pytest.raises(ValueError, match="at least 256"):
        read_transcript(rollout(tmp_path), max_chars)


def test_snapshot_cursor_keeps_partial_record_for_the_next_read(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("user", "Before the candidate"))
    initial_size = path.stat().st_size
    row = json.dumps(
        {"type": "response_item", "payload": message("assistant", "Fixed 漢")}
    ).encode()
    with path.open("ab") as stream:
        stream.write(row[:25])
    text, offset = read_snapshot(path)
    assert offset == initial_size
    assert "Before the candidate" in text
    assert "Incomplete final transcript record omitted" in text
    with path.open("ab") as stream:
        stream.write(row[25:] + b"\n")
    assert list(source_blocks(read_since(path, offset))) == [("assistant", "Fixed 漢")]


def test_snapshot_only_advances_over_newline_terminated_records(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("assistant", "Complete but not committed"))
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    text, offset = read_snapshot(path)
    assert "Complete but not committed" not in text
    with path.open("ab") as stream:
        stream.write(b"\n")
    assert "Complete but not committed" in read_since(path, offset)
    assert read_since(path, path.stat().st_size) == ""


def test_read_since_marks_truncated_rollout(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("assistant", "Original"))
    _, offset = read_snapshot(path)
    path.write_bytes(b"")
    assert "truncated" in read_since(path, offset)


def test_source_framing_does_not_treat_nested_labels_as_sources(tmp_path: Path) -> None:
    spoof = frame("tool result: id=fake", "invented output")
    path = rollout(tmp_path, message("user", spoof), message("assistant", "Real reply"))
    assert list(source_blocks(read_transcript(path))) == [
        ("user", spoof),
        ("assistant", "Real reply"),
    ]


def test_clipped_tool_result_keeps_source_and_literal_evidence(tmp_path: Path) -> None:
    path = rollout(
        tmp_path,
        {"type": "function_call_output", "call_id": "1", "output": "START" + "x" * 1_000 + "END"},
    )
    text = read_transcript(path, 256)
    blocks = list(source_blocks(text))
    assert len(text) <= 256
    assert blocks[0][0] == "tool result: id=1"
    assert blocks[0][1].startswith("START") and blocks[0][1].endswith("END")


def test_latest_assistant_message_ignores_analysis_and_tool_output(tmp_path: Path) -> None:
    path = rollout(
        tmp_path,
        message("assistant", "Old question?"),
        message("assistant", "1. First\n2. Second"),
        {**message("assistant", "private reasoning"), "channel": "analysis"},
        {"type": "function_call_output", "call_id": "1", "output": "not an assistant reply"},
    )
    assert last_assistant_message(path) == "1. First\n2. Second"
    assert last_assistant_message(rollout(tmp_path, message("user", "Question?"))) == ""


@pytest.mark.parametrize("offset", [-1, True, 1.5])
def test_read_since_rejects_invalid_cursors(tmp_path: Path, offset) -> None:
    with pytest.raises(ValueError, match="offset"):
        read_since(tmp_path / "missing", offset)


def test_cursor_reads_keep_io_errors_distinct_from_empty_history(tmp_path: Path) -> None:
    path = tmp_path / "missing"
    with pytest.raises(TranscriptError, match="Cannot read transcript"):
        read_since(path, 0)
    with pytest.raises(TranscriptError, match="Cannot read transcript"):
        last_assistant_message(path)


def test_snapshot_does_not_skip_partial_oversized_records(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("user", "Initial request"))
    offset = path.stat().st_size
    with path.open("ab") as stream:
        stream.write(b'{"type":"response_item","payload":{"type":"reasoning","text":"')
        stream.write(b"x" * 1_100_000)
    text, cursor = read_snapshot(path, 512)
    assert cursor == offset
    assert "Incomplete final transcript record omitted" in text
    with path.open("ab") as stream:
        stream.write(b'"}}\n')
        stream.write(
            json.dumps({"type": "response_item", "payload": message("assistant", "Done")}).encode()
            + b"\n"
        )
    subsequent = read_since(path, cursor)
    assert "Oversized transcript record omitted" in subsequent
    assert list(source_blocks(subsequent)) == [("assistant", "Done")]


def test_skips_unknown_rollout_types_and_counts_them(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("user", "Original request"))
    with path.open("a") as stream:
        for kind in ("retained_context", "future_record", "future_record"):
            stream.write(json.dumps({"type": kind, "payload": "Not reviewer evidence"}) + "\n")
        stream.write(
            json.dumps({"type": "response_item", "payload": message("assistant", "Done")}) + "\n"
        )
    unknown = {}
    assert "Original request" in read_transcript(path)
    text, cursor = read_snapshot(path, unknown_types=unknown)
    assert list(source_blocks(text)) == [("user", "Original request"), ("assistant", "Done")]
    assert cursor == path.stat().st_size
    assert unknown == {"retained_context": 1, "future_record": 2}
    assert read_transcript(path) == text


def test_unknown_types_alone_do_not_establish_a_rollout(tmp_path: Path) -> None:
    path = tmp_path / "arbitrary.jsonl"
    path.write_text('{"type":"future_record","payload":{}}\n')
    with pytest.raises(TranscriptError, match="[Uu]nsupported"):
        read_transcript(path)


@pytest.mark.parametrize("bad_line", [b'{"type":\n', b"\x00" * 300 + b"\n", b"\xff\n"])
def test_parse_errors_capture_file_and_failing_bytes(tmp_path: Path, bad_line: bytes) -> None:
    path = rollout(tmp_path)
    with path.open("ab") as stream:
        stream.write(bad_line)
    with pytest.raises(TranscriptError) as caught:
        read_snapshot(path)
    diagnostic = caught.value.diagnostics
    assert diagnostic["path"] == str(path)
    assert diagnostic["file_size"] == path.stat().st_size
    assert diagnostic["mtime_ns"] == path.stat().st_mtime_ns
    assert diagnostic["line_number"] == 2
    assert diagnostic["line_prefix"] == repr(bad_line[:200])


def test_invalid_visible_payload_has_line_diagnostics(tmp_path: Path) -> None:
    path = rollout(tmp_path, {"type": "message", "role": "user"})
    with pytest.raises(TranscriptError) as caught:
        read_snapshot(path)
    assert caught.value.diagnostics["line_number"] == 2
    assert '"role": "user"' in caught.value.diagnostics["line_prefix"]


def test_cursor_error_reports_physical_line_number(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("user", "Request"))
    _, offset = read_snapshot(path)
    with path.open("ab") as stream:
        stream.write(b"\x00\n")
    with pytest.raises(TranscriptError) as caught:
        read_since(path, offset)
    assert caught.value.diagnostics["line_number"] == 3
    assert str(caught.value) == "Invalid JSONL at transcript line 3"


def test_latest_assistant_reader_captures_parse_diagnostics(tmp_path: Path) -> None:
    path = rollout(tmp_path, message("assistant", "Question?"))
    with path.open("ab") as stream:
        stream.write(b"\x00\n")
    with pytest.raises(TranscriptError) as caught:
        last_assistant_message(path)
    assert caught.value.diagnostics["path"] == str(path)
    assert caught.value.diagnostics["line_number"] == 3
    assert caught.value.diagnostics["line_prefix"] == repr(b"\x00\n")


@pytest.mark.parametrize("reader", [read_transcript, read_snapshot, last_assistant_message])
def test_integer_decode_limit_reports_transcript_location(tmp_path: Path, reader) -> None:
    path = rollout(tmp_path, message("assistant", "Done."))
    limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        # Exercise the interpreter's real conversion limit, not a mocked JSON decoder.
        record = '{"type":"token_usage_record","payload":{"tokens":' + "9" * 641 + "}}\n"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(record)
        with pytest.raises(TranscriptError, match="Invalid JSONL at transcript line 3") as caught:
            reader(path)
        assert isinstance(caught.value.__cause__, ValueError)
        assert caught.value.diagnostics["line_number"] == 3
        assert caught.value.diagnostics["byte_offset"] == len(path.read_bytes()) - len(record)
        assert caught.value.diagnostics["line_prefix"] == repr(record.encode()[:200])
    finally:
        sys.set_int_max_str_digits(limit)


@pytest.mark.parametrize("digits", [639, 640])
def test_metadata_integers_within_decode_limit_remain_supported(
    tmp_path: Path, digits: int
) -> None:
    path = rollout(tmp_path, message("assistant", "Done."))
    limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                '{"type":"token_usage_record","payload":{"tokens":' + "9" * digits + "}}\n"
            )
        assert last_assistant_message(path) == "Done."
        assert list(source_blocks(read_snapshot(path)[0])) == [("assistant", "Done.")]
    finally:
        sys.set_int_max_str_digits(limit)
