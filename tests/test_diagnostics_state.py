import json

import pytest

from you_should_know.assessment import Finding
from you_should_know.state import State


@pytest.fixture
def state(tmp_path):
    instance = State(tmp_path / "state")
    try:
        yield instance
    finally:
        instance.close()


def finding(topic):
    return Finding(topic, f"Summary for {topic}", "Private explanation", ["private evidence"])


def test_no_finding_totals_scope_history_without_guessing_legacy_reasons(state, tmp_path):
    project = str(tmp_path / "project")
    state.observe("here", False, cwd=project)
    state.observe("there", False, cwd=str(tmp_path / "other"))
    for session, category in (
        ("here", "no_concern"),
        ("here", "no_concern"),
        ("here", "insufficient_evidence"),
        ("here", None),
        ("there", "already_disclosed"),
    ):
        diagnostics = (
            {"no_finding": {"category": category, "reason": "A short decision summary."}}
            if category
            else {}
        )
        state.record_check(
            session, "Stop", "clear", 100, 1, diagnostics=diagnostics, model_called=True
        )
    state.record_check("here", "Stop", "skipped", 101, 0)
    expected = [
        {"category": "no_concern", "total": 2},
        {"category": None, "total": 1},
        {"category": "insufficient_evidence", "total": 1},
    ]
    report = state.diagnostics(cwd=project, limit=1)
    assert report["no_finding_reasons"] == expected
    assert len(report["recent_checks"]) == 1
    assert state.diagnostics(cwd=project, include_skipped=True)["no_finding_reasons"] == expected
    assert state.diagnostics(cwd=str(tmp_path / "missing"))["no_finding_reasons"] == []
    assert sum(item["total"] for item in state.diagnostics()["no_finding_reasons"]) == 5


def test_candidate_listing_is_compact_and_distinguishes_history(state, tmp_path):
    directory = str(tmp_path / "project")
    state.observe("s", False, cwd=directory)
    identifiers = {}
    for index, status in enumerate(("pending", "confirmed", "suppressed", "withdrawn")):
        identifier = state.candidate(
            "s",
            finding(status),
            index + 10,
            "private transcript",
            55,
            checkpoint={"path": "/private/transcript", "sha256": "abc"},
        )
        identifiers[status] = identifier
        state.resolve_candidate(identifier, status, f"reason {status}", index + 20)

    assert state.notes() == []
    pending = state.candidates()
    assert pending == [
        {
            "id": identifiers["pending"],
            "session_id": "s",
            "cwd": directory,
            "status": "pending",
            "reason": "reason pending",
            "topic": "pending",
            "summary": "Summary for pending",
            "created_at": 10,
            "touched_at": 20,
        }
    ]
    history = state.candidates(all_candidates=True)
    assert [row["status"] for row in history] == ["withdrawn", "suppressed", "confirmed", "pending"]
    assert {row["id"] for row in history} == set(identifiers.values())
    assert all(set(row) == set(pending[0]) for row in history)


def test_scoped_status_filters_every_session_derived_section(state, tmp_path):
    first = str(tmp_path / "first")
    second = str(tmp_path / "second")
    for session, directory, multiplier in (("ours", first, 1), ("other", second, 10)):
        state.hook_seen(session, "Stop", directory)
        state.mute(session)
        state.mute(session, directory, project=True)
        state.candidate(
            session, finding(session), 100, "transcript", 10, reason=f"reason {session}"
        )
        for _ in range(3):
            state.record_transcript_read(session, failed=True)
        state.record_check(
            session,
            "Stop",
            "error",
            100,
            multiplier,
            usage={"input_tokens": multiplier, "output_tokens": 2 * multiplier},
            model=f"model-{session}",
            model_source="config",
            diagnostics={
                "transcript_errors": [{"path": f"/{session}/transcript"}],
                "evidence_rejection": {"reason": f"rejected {session}"},
            },
        )
    state.record_check("ours", "PostToolUse", "skipped", 101, 0, reason="cadence")

    assert len(state.status()) == 2
    assert len(state.diagnostics()["hooks"]) == 2
    assert {row["session_id"] for row in state.status(first)} == {"ours"}
    report = state.diagnostics(first)
    assert report["total_checks"] == 2
    assert report["model_calls"] == 1
    assert report["errors"] == 1
    assert report["input_tokens"] == 1
    assert report["output_tokens"] == 2
    assert report["latency_seconds"] == 1
    assert report["average_latency"] == 1
    assert report["candidates"] == {"pending": 1, "confirmed": 0, "suppressed": 0, "withdrawn": 0}
    assert report["candidate_reasons"] == [
        {"status": "pending", "reason": "reason ours", "total": 1}
    ]
    for field in (
        "recent_checks",
        "recent_transcript_errors",
        "recent_evidence_rejections",
        "unreviewable_sessions",
        "warnings",
    ):
        assert report[field]
        assert {row["session_id"] for row in report[field]} == {"ours"}
    assert set(report["hooks"]) == {"ours"}
    assert [row["model"] for row in report["model_usage"]] == ["model-ours"]
    assert {(row["scope"], row["target"]) for row in report["mutes"]} == {
        ("session", "ours"),
        ("project", first),
    }
    assert "other" not in json.dumps(report)


def test_directory_scope_follows_recorded_session_cwd_and_legacy_aliases(state, tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    state.hook_seen("s", "Stop", str(alias))
    assert state.session("s").cwd == str(directory)
    note = state.save("s", finding("saved"), 100)
    candidate = state.candidate("s", finding("pending"), 101, "transcript", 1)
    state.record_check("s", "Stop", "pending", 101, 1, candidate_id=candidate)
    with state.connection:
        state.connection.execute("UPDATE sessions SET cwd = ?", (str(alias),))
    assert [row["id"] for row in state.notes(cwd=str(directory))] == [note]
    assert [row["id"] for row in state.candidates(str(alias))] == [candidate]
    assert state.diagnostics(str(directory))["total_checks"] == 1
    state.observe("s", False)
    assert state.session("s").cwd == str(alias)
    next_directory = str(tmp_path / "new")
    state.observe("s", False, cwd=next_directory)
    assert state.notes(cwd=str(directory)) == []
    assert state.candidates(str(directory)) == []
    assert state.diagnostics(str(directory))["total_checks"] == 0
    assert state.diagnostics(next_directory)["total_checks"] == 1
    assert state.notes(cwd=next_directory)[0]["cwd"] == next_directory


@pytest.mark.parametrize("suffix", ["", "child"])
def test_unknown_or_invalid_directories_are_visible_only_without_scope(state, tmp_path, suffix):
    for session in ("unknown", "invalid"):
        state.observe(session, False)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with state.connection:
        state.connection.execute(
            "UPDATE sessions SET cwd = ? WHERE session_id = 'invalid'", (str(loop / suffix),)
        )
    for session in ("unknown", "invalid", "orphan"):
        state.save(session, finding(session), 100)
        state.candidate(session, finding(session), 100, "transcript", 1)
        state.record_check(session, "Stop", "clear", 100, 1)
    assert len(state.notes()) == 3
    assert len(state.candidates()) == 3
    assert state.diagnostics()["total_checks"] == 3
    assert {row["cwd"] for row in state.notes()} == {""}
    assert {row["cwd"] for row in state.candidates()} == {""}
    assert state.notes(cwd=str(tmp_path)) == []
    assert state.candidates(str(tmp_path)) == []
    report = state.diagnostics(str(tmp_path))
    assert report["total_checks"] == 0
    assert report["recent_checks"] == []
    with pytest.raises(ValueError, match="Cannot resolve working directory"):
        state.notes(cwd=str(loop / suffix))


def test_scope_and_skip_filter_apply_before_limit_but_totals_include_skips(state, tmp_path):
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    state.observe("ours", False, cwd=first)
    state.observe("other", False, cwd=second)
    for index in range(5):
        state.record_check("ours", "Stop", "clear", index, 1, reason=f"visible {index}")
        state.record_check("other", "Stop", "clear", index, 1, reason=f"hidden {index}")
        state.record_check("ours", "Stop", "skipped", index, 0, reason=f"skip {index}")
    report = state.diagnostics(first, limit=2)
    assert [row["reason"] for row in report["recent_checks"]] == ["visible 4", "visible 3"]
    assert report["total_checks"] == 10
    with_skipped = state.diagnostics(first, limit=2, include_skipped=True)
    assert [row["reason"] for row in with_skipped["recent_checks"]] == ["skip 4", "visible 4"]
    assert with_skipped["total_checks"] == report["total_checks"]


def test_configured_limit_applies_independently_to_error_histories(state):
    for index in range(5):
        state.record_check(
            "s",
            "Stop",
            "error",
            index,
            0,
            diagnostics={
                "transcript_errors": [{"line_number": index}],
                "evidence_rejection": {"reason": str(index)},
            },
        )
    state.record_check("s", "Stop", "clear", 6, 0)
    report = state.diagnostics(limit=2)
    assert [row["outcome"] for row in report["recent_checks"]] == ["clear", "error"]
    assert [
        row["diagnostics"]["transcript_errors"][0]["line_number"]
        for row in report["recent_transcript_errors"]
    ] == [4, 3]
    assert [
        row["diagnostics"]["evidence_rejection"]["reason"]
        for row in report["recent_evidence_rejections"]
    ] == ["4", "3"]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_history_limit_is_rejected(state, limit):
    with pytest.raises(ValueError, match="positive integer"):
        state.diagnostics(limit=limit)


def test_checks_keep_links_across_reopen_and_tolerate_expired_objects(state):
    state.observe("s", False)
    candidate = state.candidate("s", finding("topic"), 100, "private transcript", 1)
    note = state.publish_candidate(candidate, 101)
    state.record_check("s", "Stop", "finding", 200000, 1, candidate_id=candidate, note_id=note)
    reopened = State(state.directory)
    try:
        row = reopened.diagnostics()["recent_checks"][0]
        assert row["candidate_id"] == candidate
        assert row["note_id"] == note
        assert row["candidate"] == {
            "id": candidate,
            "status": "confirmed",
            "topic": "topic",
            "summary": "Summary for topic",
            "reason": "confirmed unresolved and undisclosed",
        }
        assert row["note"] == {
            "id": note,
            "status": "new",
            "topic": "topic",
            "summary": "Summary for topic",
        }
        removed = reopened.prune(1, 200000)
        assert removed["candidates"] == 1
        assert removed["notes"] == 1
        assert removed["checks"] == 0
        missing = reopened.diagnostics()["recent_checks"][0]
        assert missing["candidate_id"] == candidate
        assert missing["note_id"] == note
        assert missing["candidate"] is None
        assert missing["note"] is None
    finally:
        reopened.close()


def test_legacy_checks_gain_nullable_links_without_invented_associations(state):
    candidate = state.candidate("s", finding("topic"), 100, "transcript", 1)
    state.publish_candidate(candidate, 101)
    state.record_check("s", "Stop", "finding", 101, 1)
    with state.connection:
        state.connection.execute("ALTER TABLE checks DROP COLUMN candidate_id")
        state.connection.execute("ALTER TABLE checks DROP COLUMN note_id")
    reopened = State(state.directory)
    try:
        row = reopened.diagnostics()["recent_checks"][0]
        assert row["outcome"] == "finding"
        assert row["candidate_id"] is None
        assert row["note_id"] is None
        assert row["candidate"] is None
        assert row["note"] is None
    finally:
        reopened.close()


def test_mismatched_link_never_exposes_another_sessions_finding(state, tmp_path):
    directory = str(tmp_path / "ours")
    state.observe("ours", False, cwd=directory)
    state.observe("other", False, cwd=str(tmp_path / "other"))
    candidate = state.candidate("other", finding("foreign"), 100, "transcript", 1)
    note = state.publish_candidate(candidate, 101)
    state.record_check("ours", "Stop", "finding", 101, 1, candidate_id=candidate, note_id=note)
    row = state.diagnostics(directory)["recent_checks"][0]
    assert row["candidate"] is None
    assert row["note"] is None
    assert "foreign" not in json.dumps(row)
