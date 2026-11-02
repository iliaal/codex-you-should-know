import hashlib
import json
import os
from dataclasses import replace

import pytest

from you_should_know.assessment import Finding
from you_should_know.config import Config, ConfigError, load_config
from you_should_know.state import State, review_lock


def finding(topic="Topic A"):
    return Finding(topic, "A concrete consequence.", "Why it matters.", ["tool evidence"])


def test_dedup_normalizes_case_without_changing_legacy_note_ids(tmp_path):
    state = State(tmp_path)
    old_id = hashlib.sha256(b"Topic A").hexdigest()[:12]
    with state.connection:
        state.connection.execute(
            "INSERT INTO notes(id, session_id, topic, summary, explanation, evidence, created_at) "
            "VALUES (?, 'old', 'Topic A', 'summary', 'explanation', ?, 100)",
            (old_id, json.dumps(["tool evidence"])),
        )
    assert state.save("new", finding("topic a"), 200, notify=True) is None
    assert state.note(old_id)["topic"] == "Topic A"
    assert state.deliver("new") == ([], None)
    state.close()


def test_reply_target_expires_after_ten_minutes(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("you_should_know.state.time.time", lambda: now[0])
    state = State(tmp_path)
    identifier = state.save("session", finding(), now[0], notify=True)
    assert identifier is not None
    state.deliver("session", ready=True, turn_id="finished")
    now[0] += 601
    assert state.reply("session", "dismissed", turn_id="new") is None
    assert state.note(identifier)["status"] == "new"
    state.close()


def test_stop_arms_note_after_midturn_delivery_once_per_turn(tmp_path):
    state = State(tmp_path)
    identifier = state.save("session", finding(), 100, notify=True)
    notifications, active = state.deliver("session")
    assert len(notifications) == 1
    assert active is None
    notifications, active = state.deliver("session", ready=True, turn_id="finished")
    assert notifications == []
    assert active is not None
    assert active["id"] == identifier
    assert active["summary"] == finding().summary
    assert active["expanded"] is False
    assert state.deliver("session", ready=True, turn_id="finished") == ([], None)
    state.close()


def test_candidate_survives_restart_without_being_displayed(tmp_path):
    state = State(tmp_path)
    identifier = state.candidate("session", finding(), 100, "verbatim transcript", 123)
    assert state.notes() == []
    assert state.deliver("session", ready=True, turn_id="turn") == ([], None)
    state.close()
    reopened = State(tmp_path)
    candidate = reopened.pending_candidate("session")
    assert candidate is not None
    assert candidate["id"] == identifier
    assert candidate["evidence"] == ["tool evidence"]
    assert candidate["transcript"] == "verbatim transcript"
    assert candidate["offset"] == 123
    assert reopened.pending_candidate("other") is None
    reopened.close()


def test_candidate_preserves_assistant_claim_judgment_across_restart(tmp_path):
    state = State(tmp_path)
    identifier = state.candidate(
        "session", finding(), 100, "verbatim transcript", 123, assistant_claim=True
    )
    state.close()
    reopened = State(tmp_path)
    candidate = reopened.pending_candidate("session")
    assert candidate is not None
    assert candidate["id"] == identifier
    assert candidate["assistant_claim"] is True
    reopened.close()


def test_existing_candidate_migrates_without_assistant_claim_judgment(tmp_path):
    state = State(tmp_path)
    identifier = state.candidate("session", finding(), 100, "verbatim transcript", 123)
    with state.connection:
        state.connection.execute("ALTER TABLE candidates DROP COLUMN assistant_claim")
    state.close()
    reopened = State(tmp_path)
    candidate = reopened.pending_candidate("session")
    assert candidate is not None
    assert candidate["id"] == identifier
    assert candidate["assistant_claim"] is False
    assert candidate["status"] == "pending"
    reopened.close()


def test_candidate_suppression_withdrawal_and_retry_reasons_are_measurable(tmp_path):
    state = State(tmp_path)
    state.candidate("session", finding(), 100, "source", 1, "suppressed", "agent still working")
    identifier = state.candidate("session", finding("second"), 101, "source", 2)
    state.resolve_candidate(identifier, "pending", "confirmation budget exhausted", 102)
    candidate = state.pending_candidate("session")
    assert candidate is not None
    assert candidate["reason"] == "confirmation budget exhausted"
    state.resolve_candidate(identifier, "withdrawn", "assistant disclosed it", 103)
    assert state.pending_candidate("session") is None
    diagnostics = state.diagnostics()
    assert diagnostics["candidates"] == {
        "pending": 0,
        "confirmed": 0,
        "suppressed": 1,
        "withdrawn": 1,
    }
    assert {row["reason"] for row in diagnostics["candidate_reasons"]} == {
        "agent still working",
        "assistant disclosed it",
    }
    state.close()


def test_confirmation_publishes_once_and_commits_candidate_with_note(tmp_path):
    state = State(tmp_path)
    identifier = state.candidate("session", finding(), 100, "source", 123)
    note_id = state.publish_candidate(identifier, 101, notify=True)
    assert note_id is not None
    assert state.pending_candidate("session") is None
    assert state.diagnostics()["candidates"]["confirmed"] == 1
    assert state.publish_candidate(identifier, 102, notify=True) is None
    assert state.deliver("session") == ([{"note": state.note(note_id)}], None)
    state.close()


def test_stale_candidate_hold_cannot_reopen_a_published_candidate(tmp_path):
    state = State(tmp_path)
    identifier = state.candidate("session", finding(), 100, "source", 123)
    note_id = state.publish_candidate(identifier, 101, notify=True)
    assert note_id is not None
    changed = state.resolve_candidate(identifier, "pending", "final check disabled", 102)
    assert state.pending_candidate("session") is None
    assert changed is False
    assert state.diagnostics()["candidates"]["confirmed"] == 1
    assert state.diagnostics()["candidates"]["pending"] == 0
    assert state.deliver("session") == ([{"note": state.note(note_id)}], None)
    state.close()


def test_stale_candidate_resolution_cannot_change_a_withdrawn_candidate(tmp_path):
    state = State(tmp_path)
    identifier = state.candidate("session", finding(), 100, "source", 123)
    withdrawn = state.resolve_candidate(identifier, "withdrawn", "resolved by assistant", 101)
    stale = state.resolve_candidate(identifier, "suppressed", "stale reason", 102)
    assert state.diagnostics()["candidate_reasons"] == [
        {"status": "withdrawn", "reason": "resolved by assistant", "total": 1}
    ]
    assert withdrawn is True
    assert stale is False
    state.close()


def test_failed_confirmation_publication_preserves_pending_candidate(tmp_path):
    import sqlite3

    state = State(tmp_path)
    identifier = state.candidate("session", finding(), 100, "source", 123)
    state.connection.execute(
        "CREATE TRIGGER fail_publish BEFORE INSERT ON notifications "
        "BEGIN SELECT RAISE(ABORT, 'queue unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="queue unavailable"):
        state.publish_candidate(identifier, 101, notify=True)
    candidate = state.pending_candidate("session")
    assert candidate is not None
    assert candidate["id"] == identifier
    assert state.notes() == []
    state.close()


def test_confirmation_of_duplicate_is_recorded_without_republication(tmp_path):
    state = State(tmp_path)
    state.save("other", finding(), 99)
    identifier = state.candidate("session", finding("TOPIC A"), 100, "source", 123)
    assert state.publish_candidate(identifier, 101, notify=True) is None
    assert state.diagnostics()["candidate_reasons"] == [
        {"status": "suppressed", "reason": "duplicate topic", "total": 1}
    ]
    assert not state.has_notifications("session")
    state.close()


def test_budget_rolls_forward_and_preserves_explicit_lifetime_limit(tmp_path):
    state = State(tmp_path)
    state.observe("session", True)
    config = replace(Config(), max_checks_per_hour=2)
    assert state.budget_available("session", config, 100)
    state.begin("session", 100)
    state.begin("session", 200)
    assert not state.budget_available("session", config, 300)
    assert state.budget_available("session", config, 3700)
    assert not state.budget_available("session", replace(config, max_checks_per_session=2), 3700)
    state.observe("other", True)
    assert state.budget_available("other", config, 300)
    assert state.session("session").attempts == 2
    state.close()


def test_notices_are_once_per_session_and_configuration_revision(tmp_path):
    state = State(tmp_path)
    assert state.notice_once("one", "config:100:10")
    assert not state.notice_once("one", "config:100:10")
    assert state.notice_once("one", "config:101:10")
    assert state.notice_once("two", "config:100:10")
    state.close()


def test_telemetry_records_outcomes_latency_and_usage(tmp_path):
    state = State(tmp_path)
    state.record_check("session", "PostToolUse", "unchanged", 100, 0)
    state.record_check(
        "session",
        "Stop",
        "finding",
        101,
        1.25,
        {"input_tokens": 100, "cached_input_tokens": 75, "output_tokens": 5},
        phase="confirmation",
    )
    state.record_check("session", "Stop", "error", 102, 0.5, reason="timed out")
    report = state.diagnostics()
    assert report["total_checks"] == 3
    assert report["errors"] == 1
    assert report["latency_seconds"] == 1.75
    assert report["average_latency"] == 0.875
    assert (report["input_tokens"], report["cached_input_tokens"], report["output_tokens"]) == (
        100,
        75,
        5,
    )
    assert [item["outcome"] for item in report["recent_checks"]] == [
        "error",
        "finding",
        "unchanged",
    ]
    assert report["recent_checks"][1]["phase"] == "confirmation"
    state.close()


def test_telemetry_distinguishes_partial_usage_from_reported_zero_tokens(tmp_path):
    state = State(tmp_path)
    state.record_check("session", "Stop", "finding", 100, 1, {"input_tokens": 5})
    state.record_check("session", "Stop", "clear", 101, 1, {"input_tokens": 0, "output_tokens": 0})
    report = state.diagnostics()
    assert report["model_calls"] == 2
    assert report["usage_unknown_calls"] == 1
    assert report["input_tokens"] == 5
    assert report["output_tokens"] == 0
    assert report["recent_checks"][0]["usage_reported"] is True
    assert report["recent_checks"][1]["usage_reported"] is False
    state.close()


def test_telemetry_counts_calls_without_usage_and_excludes_noop_checks(tmp_path):
    state = State(tmp_path)
    state.record_check("session", "Stop", "error", 100, 1, model_called=True)
    state.record_check("session", "Stop", "clear", 101, 1, {}, model_called=True)
    state.record_check("session", "Stop", "unchanged", 102, 0)
    state.record_check("session", "Stop", "skipped", 103, 0, {})
    report = state.diagnostics(include_skipped=True)
    assert report["total_checks"] == 4
    assert report["model_calls"] == 2
    assert report["usage_unknown_calls"] == 2
    assert report["input_tokens"] == 0
    assert report["output_tokens"] == 0
    assert [check["model_called"] for check in report["recent_checks"]] == [
        False,
        False,
        True,
        True,
    ]
    state.close()


def test_telemetry_migration_preserves_legacy_totals_without_inventing_call_counts(tmp_path):
    state = State(tmp_path)
    state.record_check("session", "Stop", "error", 100, 1)
    with state.connection:
        state.connection.execute("ALTER TABLE checks DROP COLUMN model_called")
        state.connection.execute("ALTER TABLE checks DROP COLUMN usage_reported")
    state.close()
    reopened = State(tmp_path)
    report = reopened.diagnostics()
    assert report["total_checks"] == 1
    assert report["errors"] == 1
    assert report["model_calls"] == 0
    assert report["usage_unknown_calls"] == 0
    reopened.close()


def test_model_telemetry_migrates_without_inventing_legacy_models(tmp_path):
    state = State(tmp_path)
    state.record_check("old", "Stop", "clear", 100, 1, model_called=True)
    with state.connection:
        state.connection.execute("ALTER TABLE checks DROP COLUMN model")
        state.connection.execute("ALTER TABLE checks DROP COLUMN model_source")
    state.close()

    reopened = State(tmp_path)
    report = reopened.diagnostics()
    assert report["recent_checks"][0]["model"] is None
    assert report["recent_checks"][0]["model_source"] is None
    assert report["model_usage"][0]["model"] is None
    assert report["model_usage"][0]["model_calls"] == 1
    reopened.close()


def test_model_usage_groups_calls_and_excludes_skipped_checks(tmp_path):
    state = State(tmp_path)
    for outcome, usage in (
        ("clear", {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}),
        ("error", {}),
    ):
        state.record_check(
            "s",
            "Stop",
            outcome,
            100,
            1,
            usage,
            model_called=True,
            model="gpt-6-astra",
            model_source="hook",
        )
    state.record_check("s", "Stop", "skipped", 101, 0, model="gpt-6-astra", model_source="hook")
    report = state.diagnostics(include_skipped=True)
    assert report["model_usage"] == [
        {
            "model": "gpt-6-astra",
            "model_source": "hook",
            "model_calls": 2,
            "errors": 1,
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "output_tokens": 5,
            "usage_unknown_calls": 1,
        }
    ]
    assert report["recent_checks"][0]["model"] is None
    assert report["recent_checks"][0]["model_source"] is None
    state.close()


def test_hook_sightings_warn_only_about_events_not_observed(tmp_path, monkeypatch):
    monkeypatch.setattr("you_should_know.state.time.time", lambda: 123.0)
    state = State(tmp_path)
    assert state.diagnostics()["warnings"] == []
    state.hook_seen("session", "Stop", "/project")
    report = state.diagnostics()
    assert report["hooks"] == {"session": {"Stop": 123}}
    assert any("UserPromptSubmit never seen" in row["message"] for row in report["warnings"])
    state.hook_seen("session", "UserPromptSubmit")
    state.hook_seen("session", "PostToolUse")
    assert any(
        "PostToolUse:notify never seen" in row["message"] for row in state.diagnostics()["warnings"]
    )
    state.hook_seen("session", "PostToolUse:notify")
    assert state.diagnostics()["warnings"] == []
    assert state.session("session").cwd == "/project"
    state.close()


def test_mutes_are_scoped_to_session_or_canonical_project(tmp_path):
    state = State(tmp_path)
    state.mute("one")
    assert state.muted("one", "/a")
    assert not state.muted("two", "/a")
    state.mute("two", "/a/../project", project=True)
    assert state.muted("three", "/project")
    assert not state.muted("three", "/different")
    with pytest.raises(ValueError, match="working directory"):
        state.mute("four", project=True)
    state.close()
    reopened = State(tmp_path)
    assert reopened.muted("one")
    assert reopened.muted("new", "/project")
    reopened.close()


def test_unmute_removes_only_the_requested_scope(tmp_path):
    state = State(tmp_path)
    state.mute("one")
    state.mute("one", "/project", project=True)
    state.unmute("one")
    assert not state.muted("one", "/elsewhere")
    assert state.muted("one", "/project")
    state.unmute("one", "/project", project=True)
    assert not state.muted("one", "/project")
    with pytest.raises(ValueError, match="working directory"):
        state.unmute("one", project=True)
    state.close()


def test_legacy_reply_state_without_expiry_is_invalidated(tmp_path):
    state = State(tmp_path)
    identifier = state.save("session", finding(), 100, notify=True)
    assert identifier is not None
    state.deliver("session", ready=True, turn_id="finished")
    with state.connection:
        state.connection.execute("ALTER TABLE interactions DROP COLUMN ready_at")
    state.close()
    reopened = State(tmp_path)
    assert reopened.reply("session", "dismissed", turn_id="reply") is None
    assert reopened.note(identifier)["status"] == "new"
    reopened.close()


def test_reply_expiry_is_configurable_and_refreshes_when_details_are_requested(
    tmp_path, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr("you_should_know.state.time.time", lambda: now[0])
    state = State(tmp_path)
    identifier = state.save("session", finding(), now[0], notify=True)
    state.deliver("session", ready=True, turn_id="finished")
    now[0] += 59
    details = state.reply("session", "more", turn_id="details", timeout_seconds=60)
    assert details is not None
    assert details["id"] == identifier
    now[0] += 59
    dismissed = state.reply("session", "dismissed", turn_id="reply", timeout_seconds=60)
    assert dismissed is not None
    assert dismissed["id"] == identifier
    state.close()


def test_pruning_expires_old_notifications_in_an_active_session(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("you_should_know.state.time.time", lambda: now[0])
    state = State(tmp_path)
    state.queue_error("session", "old error")
    now[0] = 40 * 86_400
    state.observe("session", True)
    state.queue_error("session", "new error")
    report = state.prune(30, now[0])
    assert report["notifications"] == 1
    assert state.deliver("session") == ([{"message": "new error"}], None)
    state.close()


def test_pruning_keeps_an_old_note_that_was_recently_reminded(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("you_should_know.state.time.time", lambda: now[0])
    state = State(tmp_path)
    identifier = state.save("session", finding(), now[0], notify=True)
    assert identifier is not None
    state.deliver("session", ready=True, turn_id="first")
    now[0] = 40 * 86_400
    state.observe("session", True)
    state.deliver("session", ready=True, turn_id="latest")
    state.prune(30, now[0])
    assert state.note(identifier)["status"] == "new"
    response = state.reply("session", "known", turn_id="reply")
    assert response is not None
    assert response["id"] == identifier
    state.close()


def test_known_topics_outlive_recent_notes_and_recent_history_has_status(tmp_path):
    state = State(tmp_path)
    identifier = state.save("old", finding("Old known topic"), 1)
    assert identifier is not None
    state.mark(identifier, "known")
    for index in range(41):
        state.save("new", finding(f"recent {index}"), index + 2)
    assert len(state.previous()) == 40
    assert all(item["status"] == "new" for item in state.previous())
    assert state.known_topics() == ["old known topic"]
    assert state.has_topic(" OLD   KNOWN TOPIC ")
    assert not state.has_topic("brand new")
    state.close()


def test_pruning_removes_stale_state_and_unheld_locks_but_preserves_recent_notes(
    tmp_path, monkeypatch
):
    now = [100.0]
    monkeypatch.setattr("you_should_know.state.time.time", lambda: now[0])
    state = State(tmp_path)
    state.hook_seen("old", "Stop", "/old")
    identifier = state.save("old", finding("old"), 100, notify=True)
    assert identifier is not None
    state.deliver("old", ready=True, turn_id="old-turn")
    state.candidate("old", finding("candidate"), 100, "source", 1)
    state.begin("old", 100)
    state.record_check("old", "Stop", "clear", 100, 1)
    state.notice_once("old", "notice")
    state.mute("old", "/old", project=True)
    with review_lock(tmp_path, "old"):
        pass
    lock = next(tmp_path.glob("review-*.lock"))
    os.utime(lock, (100, 100))
    now[0] = 40 * 86_400
    state.hook_seen("recent", "Stop", "/recent")
    recent = state.save("recent", finding("recent"), now[0])
    assert recent is not None
    report = state.prune(30, now[0])
    assert report["notes"] == 1
    assert report["locks"] == 1
    assert report["sessions"] == 1
    assert not lock.exists()
    with pytest.raises(ValueError, match="Unknown note"):
        state.note(identifier)
    assert state.note(recent)["topic"] == "recent"
    assert state.pending_candidate("old") is None
    assert state.deliver("old", ready=True, turn_id="later") == ([], None)
    assert state.diagnostics()["total_checks"] == 0
    assert state.diagnostics()["hooks"] == {"recent": {"Stop": now[0]}}
    assert state.diagnostics()["mutes"] == []
    state.close()


def test_pruning_cannot_unlink_a_held_review_lock(tmp_path):
    state = State(tmp_path)
    with review_lock(tmp_path, "old") as acquired:
        assert acquired
        lock = next(tmp_path.glob("review-*.lock"))
        os.utime(lock, (100, 100))
        assert state.prune(30, 40 * 86_400) == {}
        assert lock.exists()
        with review_lock(tmp_path, "old") as duplicate:
            assert not duplicate
    assert state.prune(30, 40 * 86_400)["locks"] == 1
    state.close()


@pytest.mark.parametrize(
    "text",
    [
        "final_check = 1",
        "debug = 'yes'",
        "max_checks_per_hour = 0",
        "retention_days = 0",
        "reply_timeout_seconds = 0",
        "unknown = true",
        "review_instructions = 3",
        'review_instructions = "' + "x" * 8001 + '"',
    ],
)
def test_new_configuration_rejects_invalid_values_with_file_path(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=str(path)):
        load_config(tmp_path)


def test_new_configuration_defaults_and_explicit_lifetime_budget(tmp_path):
    config = load_config(tmp_path)
    assert config.max_checks_per_session is None
    assert config.max_checks_per_hour == 30
    assert config.final_check
    assert config.retention_days == 30
    assert config.reply_timeout_seconds == 600
    assert not config.debug
    assert config.review_instructions == ""
    (tmp_path / "config.toml").write_text(
        "max_checks_per_session = 12\nmax_checks_per_hour = 3\nfinal_check = false\n"
        "debug = true\nretention_days = 7\nreply_timeout_seconds = 60\n"
    )
    config = load_config(tmp_path)
    assert config.max_checks_per_session == 12
    assert config.max_checks_per_hour == 3
    assert not config.final_check
    assert config.debug
    assert config.retention_days == 7
    assert config.reply_timeout_seconds == 60


def test_config_accepts_additional_review_focus(tmp_path):
    (tmp_path / "config.toml").write_text(
        'review_instructions = """\n'
        'Check recurring costs.\nExplain the effect in plain language.\n"""\n'
    )
    assert load_config(tmp_path).review_instructions == (
        "Check recurring costs.\nExplain the effect in plain language.\n"
    )
