import io
import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from review_responses import clear_response

from you_should_know import backend, host
from you_should_know.assessment import Finding
from you_should_know.cli import check, eligible, hook_result, main
from you_should_know.config import Config, ConfigError, data_directory, load_config
from you_should_know.state import State, review_lock


@pytest.fixture(autouse=True)
def interactive_host(monkeypatch):
    monkeypatch.setattr(host.os, "getppid", lambda: 100)
    monkeypatch.setattr(host, "process_info", lambda pid: (1, "/bin/codex", ["codex"]))


@pytest.fixture(autouse=True)
def forbid_unstubbed_reviews(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Each test must stub its reviewer; live reviews are forbidden")

    monkeypatch.setattr(backend, "review", forbidden)


@pytest.fixture
def state(tmp_path):
    store = State(tmp_path / "state")
    yield store
    store.close()


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Keep the previous settings."}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Deleted settings.json and wrote an empty object.",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


@pytest.fixture
def finding():
    return Finding(
        "settings.json previous settings deleted",
        "Your previous settings were deleted.",
        "The tool output contradicts your request to preserve the settings.",
        ["Deleted settings.json and wrote an empty object."],
    )


def finding_response(finding, schema_path):
    if schema_path.name == "confirmation.schema.json":
        return {
            "confirmed": True,
            "reason": "The settings remain deleted and the assistant has not disclosed it.",
            "evidence": finding.evidence,
        }
    assert schema_path.name == "finding.schema.json"
    return {
        "no_finding": None,
        "finding": {**finding.__dict__, "agent_still_working": False, "assistant_claim": False},
    }


def test_cadence_cooldown_stop_and_limit(state):
    config = Config()
    for _ in range(5):
        session = state.observe("session", True)
        assert not eligible(session, config, "PostToolUse", 1000, False)
    assert eligible(session, config, "Stop", 1000, False)
    session = state.observe("session", True)
    assert eligible(session, config, "PostToolUse", 1000, False)
    state.begin("session", 1000)
    session = state.observe("session", True)
    assert not eligible(session, config, "PostToolUse", 1119, False)
    assert eligible(session, config, "Stop", 1001, False)
    assert eligible(session, config, "Stop", 1120, False)
    assert not eligible(session, replace(config, max_checks_per_session=1), "Stop", 2000, False)
    assert eligible(session, config, "Stop", 1001, True)
    assert not eligible(session, replace(config, enabled=False), "Stop", 2000, True)


@pytest.mark.parametrize(
    "command,event",
    [
        ("hook", "Stop"),
        ("hook", "PostToolUse"),
        ("hook", "UserPromptSubmit"),
        ("notify", "PostToolUse"),
    ],
)
def test_exec_automatic_check_does_not_create_state(
    tmp_path, transcript, monkeypatch, capsys, command, event
):
    directory = tmp_path / "unused-exec-state"
    monkeypatch.setattr(host, "process_info", lambda pid: (1, "/bin/codex", ["codex", "exec"]))
    monkeypatch.setattr(backend, "review", lambda *args, **kwargs: clear_response())
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "headless",
                    "hook_event_name": event,
                    "transcript_path": str(transcript),
                }
            )
        ),
    )
    assert main(["--data-dir", str(directory), command]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert not directory.exists()


def test_exec_does_not_consume_pending_notes(state, transcript, finding, monkeypatch, capsys):
    identifier = state.save("resumed", finding, 100, notify=True)
    monkeypatch.setattr(
        host, "process_info", lambda pid: (1, "/bin/codex", ["codex", "exec", "resume"])
    )
    output = run_hook(
        state,
        monkeypatch,
        capsys,
        {
            "session_id": "resumed",
            "hook_event_name": "Stop",
            "turn_id": "headless-turn",
            "transcript_path": str(transcript),
        },
    )
    assert output == {}
    pending, active = state.deliver("resumed", ready=True, turn_id="interactive-turn")
    assert any(item.get("note", {}).get("id") == identifier for item in pending)
    assert active["id"] == identifier


@pytest.mark.parametrize(
    "source", ["exec", {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}]
)
def test_interactive_resume_reviews_despite_historical_source(
    state, transcript, monkeypatch, capsys, source
):
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"source": source}})
        + "\n"
        + transcript.read_text()
    )
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex_exec")
    monkeypatch.setattr(
        host, "process_info", lambda pid: (1, "/bin/codex", ["codex", "resume", "s"])
    )
    monkeypatch.setattr(backend, "review", lambda *args, **kwargs: clear_response())
    assert (
        run_hook(
            state,
            monkeypatch,
            capsys,
            {
                "session_id": "s",
                "hook_event_name": "Stop",
                "agent_id": "historical-child",
                "transcript_path": str(transcript),
            },
        )
        == {}
    )
    assert state.session("s").last_success > 0
    assert state.session("s").attempts == 1


def test_manual_check_still_reviews_inside_exec(state, transcript, finding, monkeypatch, capsys):
    monkeypatch.setattr(host, "process_info", lambda pid: (1, "/bin/codex", ["codex", "exec"]))
    monkeypatch.setattr(
        backend, "review", lambda prompt, schema, **kwargs: finding_response(finding, schema)
    )
    assert (
        main(
            [
                "--data-dir",
                str(state.directory),
                "check",
                "--session",
                "explicit",
                "--transcript",
                str(transcript),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "finding"
    assert result["note"]["summary"] == finding.summary
    assert state.session("explicit").attempts == 2


def test_manual_check_saves_validated_note_and_survives_reopen(
    state,
    transcript,
    finding,
    monkeypatch,
):
    def reviewer(prompt, schema_path, model, timeout):
        assert "Keep the previous settings." in prompt
        assert schema_path.is_file()
        assert model == "session-model"
        if schema_path.name == "finding.schema.json":
            assert 89 < timeout <= 90
        else:
            assert 0 < timeout <= 90
        return finding_response(finding, schema_path)

    monkeypatch.setattr(backend, "review", reviewer)
    result = check(state, Config(), "s1", transcript, "Stop", "session-model", force=True)
    assert result["status"] == "finding"
    note = result["note"]
    assert note["evidence"] == finding.evidence
    assert len(note["id"]) == 12
    assert set(hook_result(result)) == {"systemMessage"}
    other = State(state.directory)
    try:
        assert other.note(note["id"])["summary"] == finding.summary
        assert other.session("s1").last_success > 0
        assert other.session("s1").last_error == ""
    finally:
        other.close()


def test_duplicate_across_sessions_and_disposition(state, finding):
    identifier = state.save("one", finding, 100)
    assert identifier is not None
    assert state.save("two", replace(finding, topic="  " + finding.topic + "  "), 200) is None
    assert len(state.notes()) == 1
    state.mark(identifier, "known")
    assert state.notes() == []
    assert state.notes(True)[0]["status"] == "known"
    history = state.previous()
    assert len(history) == 1
    assert {key: history[0][key] for key in finding.__dict__} == finding.__dict__
    assert history[0]["status"] == "known"
    state.mark(identifier, "dismissed")
    assert state.note(identifier)["status"] == "dismissed"
    assert state.save("three", finding, 300) is None
    with pytest.raises(ValueError, match="Unknown note"):
        state.mark("missing", "known")
    with pytest.raises(ValueError, match="Invalid note status"):
        state.mark(identifier, "invalid")


def test_distinct_path_separators_remain_distinct_but_topic_case_deduplicates(state, finding):
    identifiers = [
        state.save("session", replace(finding, topic=topic), 100)
        for topic in (
            "src/a-b.py lost changes",
            "src/a/b.py lost changes",
            "src/A-b.py lost changes",
        )
    ]
    assert identifiers[0] is not None
    assert identifiers[1] is not None
    assert identifiers[0] != identifiers[1]
    assert identifiers[2] is None
    assert len(state.notes()) == 2


def test_other_sessions_can_review_while_one_is_busy(state, transcript, monkeypatch):
    monkeypatch.setattr(backend, "review", lambda *args, **kwargs: clear_response())
    with review_lock(state.directory, "first-session") as locked:
        assert locked
        result = check(state, Config(), "second-session", transcript, "Stop", force=True)
    assert result["status"] == "clear"


def test_stop_checks_conversation_without_tool_calls(state, transcript, monkeypatch):
    monkeypatch.setattr(backend, "review", lambda *args, **kwargs: clear_response())
    result = check(state, Config(), "s", transcript, "Stop")
    assert result["status"] == "clear"
    assert state.session("s").events == 0


def test_stop_checks_later_assistant_message_during_background_cooldown(
    state, transcript, monkeypatch
):
    calls = []

    def reviewer(*args, **kwargs):
        calls.append(args[0])
        return clear_response()

    monkeypatch.setattr(backend, "review", reviewer)
    config = replace(Config(), every_n_tools=1)
    assert check(state, config, "s", transcript, "PostToolUse")["status"] == "clear"
    with transcript.open("a") as file:
        file.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The final answer claims every setting was preserved.",
                            }
                        ],
                    },
                }
            )
            + "\n"
        )
    assert check(state, config, "s", transcript, "Stop")["status"] == "clear"
    assert len(calls) == 2
    assert "The final answer claims every setting was preserved." in calls[1]


def test_lock_waits_for_same_session_and_has_a_deadline(state):
    ready, release = threading.Event(), threading.Event()

    def worker():
        with review_lock(state.directory, "same"):
            ready.set()
            release.wait(2)

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(2)
    try:
        with review_lock(state.directory, "same", 0.01) as acquired:
            assert not acquired
        release.set()
        with review_lock(state.directory, "same", 2) as acquired:
            assert acquired
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_busy_worker_does_not_spend_check_budget(state, transcript, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("busy check must not invoke a model")

    monkeypatch.setattr(backend, "review", forbidden)
    with review_lock(state.directory, "s1") as locked:
        assert locked
        result = check(state, Config(), "s1", transcript, "Stop", force=True)
    assert result["status"] == "skipped"
    assert result["reason"] == "review is busy"
    assert result["latency"] >= 0
    assert result["usage"] == {}
    recorded = state.diagnostics(include_skipped=True)["recent_checks"][0]
    assert recorded["outcome"] == "skipped"
    assert recorded["reason"] == "review is busy"
    assert recorded["latency"] == result["latency"]
    assert state.session("s1").attempts == 0
    with review_lock(state.directory, "s1") as locked:
        assert locked


def test_cleared_and_unchanged_are_distinct_from_failure(state, transcript, monkeypatch):
    calls = []

    def reviewer(*args, **kwargs):
        calls.append(True)
        return clear_response()

    monkeypatch.setattr(backend, "review", reviewer)
    config = replace(Config(), min_interval_seconds=0, every_n_tools=1)
    assert check(state, config, "s", transcript, "PostToolUse")["status"] == "clear"
    assert check(state, config, "s", transcript, "PostToolUse")["status"] == "unchanged"
    assert len(calls) == 1
    assert state.notes() == []


def test_backend_failure_is_recorded_without_repeated_notifications(state, transcript, monkeypatch):
    def failed(*args, **kwargs):
        raise backend.BackendError("Codex review timed out")

    monkeypatch.setattr(backend, "review", failed)
    first = check(state, Config(), "s", transcript, "Stop", force=True)
    assert first["status"] == "error"
    assert "failed" in hook_result(first)["systemMessage"]
    assert state.session("s").last_error == "Codex review timed out"
    assert state.session("s").last_success == 0
    second = check(state, Config(), "s", transcript, "Stop", force=True)
    assert hook_result(second) == {}


def test_invalid_assessment_never_becomes_a_clear_check(state, transcript, monkeypatch):
    monkeypatch.setattr(backend, "review", lambda *args, **kwargs: {})
    result = check(state, Config(), "s", transcript, "Stop", force=True)
    assert result["status"] == "error"
    assert state.session("s").last_success == 0


def test_hook_protocol_and_cli_note_actions(state, transcript, finding, monkeypatch, capsys):
    monkeypatch.setattr(
        backend, "review", lambda prompt, schema, **kwargs: finding_response(finding, schema)
    )
    (state.directory / "config.toml").write_text("every_n_tools = 1\n")
    event = {
        "session_id": "s",
        "hook_event_name": "PostToolUse",
        "transcript_path": str(transcript),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    prefix = ["--data-dir", str(state.directory)]
    assert main([*prefix, "hook"]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main([*prefix, "notify"]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert state.notes() == []
    assert state.pending_candidate("s") is not None
    event.update(hook_event_name="Stop", turn_id="finished-turn")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main([*prefix, "hook"]) == 0
    warning = json.loads(capsys.readouterr().out)
    assert set(warning) == {"systemMessage"}
    assert finding.summary in warning["systemMessage"]
    assert main([*prefix, "notes"]) == 0
    identifier = json.loads(capsys.readouterr().out)["notes"][0]["id"]
    assert main([*prefix, "show", identifier]) == 0
    assert json.loads(capsys.readouterr().out)["evidence"] == finding.evidence
    assert main([*prefix, "know", identifier]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "known"
    assert main([*prefix, "dismiss", identifier]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dismissed"
    assert main([*prefix, "status"]) == 0
    assert json.loads(capsys.readouterr().out)["sessions"][0]["attempts"] == 2


def run_hook(state, monkeypatch, capsys, event, command="hook"):
    if "transcript_path" not in event:
        path = state.directory / "completed-turn.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {"type": "message", "role": "assistant", "content": "Done."},
                }
            )
            + "\n"
        )
        event = {**event, "transcript_path": str(path)}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    assert main(["--data-dir", str(state.directory), command]) == 0
    return json.loads(capsys.readouterr().out)


def test_stop_delivers_background_finding_even_when_review_is_on_cooldown(
    state, transcript, finding, monkeypatch, capsys
):
    started, release = threading.Event(), threading.Event()

    def reviewer(prompt, schema, **kwargs):
        started.set()
        assert release.wait(3)
        return finding_response(finding, schema)

    monkeypatch.setattr(backend, "review", reviewer)
    (state.directory / "config.toml").write_text("every_n_tools = 1\n")
    outcomes = []

    def background():
        worker = State(state.directory)
        try:
            outcomes.append(
                check(worker, replace(Config(), every_n_tools=1), "s", transcript, "PostToolUse")
            )
        finally:
            worker.close()

    thread = threading.Thread(target=background)
    thread.start()
    assert started.wait(3)
    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        event = {
            "session_id": "s",
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
            "turn_id": "finished-turn",
        }
        output = run_hook(state, monkeypatch, capsys, event)
    finally:
        release.set()
        timer.join(3)
        thread.join(3)
    assert not thread.is_alive()
    assert outcomes[0]["status"] == "pending"
    assert finding.summary in output["systemMessage"]
    for option in (
        "[1] Tell me more",
        "[2] Already knew that",
        "[3] Dismiss",
    ):
        assert option in output["systemMessage"]
    assert "Chat in main session" not in output["systemMessage"]
    assert "4:" not in output["systemMessage"]
    assert set(output) == {"systemMessage"}
    assert state.session("s").attempts == 2
    assert run_hook(state, monkeypatch, capsys, event) == {}


@pytest.mark.parametrize("more,dismiss", [("Tell me more!", "dismiss"), ("1", "3")])
def test_organic_replies_need_no_skill_or_main_agent_context(
    state, finding, monkeypatch, capsys, more, dismiss
):
    identifier = state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=True, turn_id="finished-turn")
    event = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": more,
        "turn_id": "reply-turn",
    }
    details = run_hook(state, monkeypatch, capsys, event)
    assert set(details) == {"continue", "systemMessage"}
    assert details["continue"] is False
    assert finding.explanation in details["systemMessage"]
    assert all(quote in details["systemMessage"] for quote in finding.evidence)
    assert "[3] Dismiss" in details["systemMessage"]
    assert "[4] Chat in main session" in details["systemMessage"]
    assert 'reply "4 your message"' in details["systemMessage"]
    assert "[1]" not in details["systemMessage"]
    assert "[2]" not in details["systemMessage"]
    assert "After the turn ends" not in details["systemMessage"]
    assert "$you-should-know" not in details["systemMessage"]
    event["prompt"] = dismiss
    confirmation = run_hook(state, monkeypatch, capsys, event)
    assert confirmation["continue"] is False
    assert "dismissed" in confirmation["systemMessage"]
    assert state.note(identifier)["status"] == "dismissed"
    assert run_hook(state, monkeypatch, capsys, event) == {}


@pytest.mark.parametrize("prompt", ["2", "already knew that", "YSK KNOWN!"])
def test_summary_can_be_marked_known_without_reading_details(
    state, finding, monkeypatch, capsys, prompt
):
    identifier = state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=True, turn_id="finished-turn")
    event = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "turn_id": "known-turn",
    }
    confirmation = run_hook(state, monkeypatch, capsys, event)
    assert confirmation["continue"] is False
    assert "already known" in confirmation["systemMessage"]
    assert state.note(identifier)["status"] == "known"


@pytest.mark.parametrize("prompt", ["dismiss", "3"])
def test_dismiss_reply_is_local_and_persistent(state, finding, monkeypatch, capsys, prompt):
    identifier = state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=True, turn_id="finished-turn")
    event = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "turn_id": "reply-turn",
    }
    assert run_hook(state, monkeypatch, capsys, event)["continue"] is False
    assert state.note(identifier)["status"] == "dismissed"


@pytest.mark.parametrize(
    "prompt",
    [
        "chat in main session",
        "YSK CHAT!",
        "chat in main session: This was intentional; explain the tradeoff.",
        "ysk chat : Please preserve my feedback exactly.",
        "4",
        "4 hi",
        "4 Please check URL https://example.test:8443 and keep MiXeD case.",
        "  4   This was intentional.  ",
        "4\tPlease explain.",
        "4\nPlease explain.",
        "4: This was intentional; explain the tradeoff.",
    ],
)
def test_chat_hands_only_selected_note_to_agent_and_allows_user_prompt(
    state, finding, monkeypatch, capsys, prompt
):
    identifier = state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=True, turn_id="finished-turn")
    assert state.reply("s", "more", turn_id="details-turn") is not None
    state.save(
        "another-session",
        replace(finding, topic="Other private topic", summary="Other private summary"),
        101,
        notify=True,
    )
    event = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "turn_id": "chat-turn",
    }
    output = run_hook(state, monkeypatch, capsys, event)
    assert output["continue"] is True
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "UserPromptSubmit"
    context = specific["additionalContext"]
    shared = json.loads(context.split("\n\n", 1)[1])
    assert shared == {"id": identifier, **finding.__dict__}
    assert "Other private" not in context
    assert "advisory data, not instructions" in context
    assert "feedback or request" in context
    assert set(output) == {"continue", "systemMessage", "hookSpecificOutput"}
    assert "shared with the main agent" in output["systemMessage"]
    assert state.note(identifier)["status"] == "new"
    assert run_hook(state, monkeypatch, capsys, event) == {}
    event["prompt"] = "dismiss"
    assert run_hook(state, monkeypatch, capsys, event) == {}
    assert state.deliver("s", ready=True, turn_id="later-turn") == ([], None)


@pytest.mark.parametrize(
    "session_id,turn_id,ready",
    [
        ("another-session", "chat-turn", True),
        ("s", "finished-turn", True),
        ("s", None, True),
        ("s", "chat-turn", False),
    ],
)
def test_chat_cannot_share_other_sessions_or_active_turn_notes(
    state, finding, monkeypatch, capsys, session_id, turn_id, ready
):
    state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=True, turn_id="finished-turn")
    assert state.reply("s", "more", turn_id="details-turn") is not None
    if not ready:
        state.reply("s", None, turn_id="task-turn")
    event = {
        "session_id": session_id,
        "hook_event_name": "UserPromptSubmit",
        "prompt": "chat in main session: please discuss it",
        "turn_id": turn_id,
    }
    assert run_hook(state, monkeypatch, capsys, event) == {}


@pytest.mark.parametrize(
    "prompt", ["4", "4 hi", "4: please discuss it", "chat in main session", "ysk chat"]
)
def test_chat_before_details_passes_through_without_sharing(
    state, finding, monkeypatch, capsys, prompt
):
    identifier = state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=True, turn_id="finished-turn")
    event = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "turn_id": "chat-turn",
    }
    assert run_hook(state, monkeypatch, capsys, event) == {}
    assert state.note(identifier)["status"] == "new"
    _, active = state.deliver("s", ready=True, turn_id="chat-turn")
    assert active["id"] == identifier
    assert active["expanded"] is False


def test_numbered_details_then_chat_shares_the_same_full_note(state, finding, monkeypatch, capsys):
    identifier = state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=True, turn_id="finished-turn")
    event = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "1",
        "turn_id": "details-turn",
    }
    details = run_hook(state, monkeypatch, capsys, event)
    assert details["continue"] is False
    event.update(
        prompt="4: This was intentional. Does the concern still apply?", turn_id="chat-turn"
    )
    handoff = run_hook(state, monkeypatch, capsys, event)
    assert handoff["continue"] is True
    context = handoff["hookSpecificOutput"]["additionalContext"]
    shared = json.loads(context.split("\n\n", 1)[1])
    assert shared == {"id": identifier, **finding.__dict__}
    assert shared["explanation"] in details["systemMessage"]
    assert all(quote in details["systemMessage"] for quote in shared["evidence"])
    assert state.note(identifier)["status"] == "new"
    assert run_hook(state, monkeypatch, capsys, event) == {}


@pytest.mark.parametrize("prompt", ["1", "2", "3", "4", "4 hi", "4: please discuss it"])
@pytest.mark.parametrize(
    "ready,turn_id", [(False, "next-turn"), (True, "finished-turn"), (True, None)]
)
def test_numbered_replies_pass_through_when_note_actions_are_inactive(
    state, finding, monkeypatch, capsys, prompt, ready, turn_id
):
    event = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "turn_id": turn_id,
    }
    assert run_hook(state, monkeypatch, capsys, event) == {}
    identifier = state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=ready, turn_id="finished-turn")
    assert run_hook(state, monkeypatch, capsys, event) == {}
    assert state.note(identifier)["status"] == "new"


@pytest.mark.parametrize(
    "prompt",
    [
        "more",
        "dismiss the dialog",
        "tell me more and fix it",
        "new task",
        None,
        "implement chat in main session",
        "chat in main session and fix it",
        "0",
        "5",
        "12",
        "40 hi",
        "4hi",
        "4. hi",
        "4/hi",
        "1 more thing",
        "1: please explain",
        "2: already knew",
        "3: dismiss this",
    ],
)
@pytest.mark.parametrize("expanded", [False, True])
def test_ordinary_prompts_clear_reply_target_and_pass_through(
    state, finding, monkeypatch, capsys, prompt, expanded
):
    identifier = state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=True, turn_id="finished-turn")
    if expanded:
        assert state.reply("s", "more", turn_id="details-turn") is not None
    event = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "turn_id": "reply-turn",
    }
    assert run_hook(state, monkeypatch, capsys, event) == {}
    event["prompt"] = "dismiss"
    assert run_hook(state, monkeypatch, capsys, event) == {}
    assert state.note(identifier)["status"] == "new"


@pytest.mark.parametrize("expanded", [False, True])
def test_ordinary_work_ends_with_a_compact_reminder_even_when_checks_are_capped(
    state, transcript, finding, monkeypatch, capsys, expanded
):
    (state.directory / "config.toml").write_text("max_checks_per_session = 1\n")
    state.observe("s", False)
    state.begin("s", 100)
    identifier = state.save("s", finding, 100, notify=True)
    stop = {
        "session_id": "s",
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
        "turn_id": "first-turn",
    }
    first = run_hook(state, monkeypatch, capsys, stop)
    assert "[1] Tell me more\n[2] Already knew that" in first["systemMessage"]
    assert "[4]" not in first["systemMessage"]
    if expanded:
        details_event = {
            "session_id": "s",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "1",
            "turn_id": "details-turn",
        }
        assert run_hook(state, monkeypatch, capsys, details_event)["continue"] is False
    ordinary = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "Explain the next task.",
        "turn_id": "next-turn",
    }
    assert run_hook(state, monkeypatch, capsys, ordinary) == {}
    stop["turn_id"] = "next-turn"
    reminder = run_hook(state, monkeypatch, capsys, stop)
    assert set(reminder) == {"systemMessage"}
    assert finding.summary in reminder["systemMessage"]
    if expanded:
        assert "[3] Dismiss · [4] Chat in main session" in reminder["systemMessage"]
        assert 'reply "4 your message"' in reminder["systemMessage"]
        assert "[1]" not in reminder["systemMessage"]
        assert "[2]" not in reminder["systemMessage"]
    else:
        assert "[1] Tell me more · [2] Already knew that" in reminder["systemMessage"]
        assert "[4]" not in reminder["systemMessage"]
    assert finding.explanation not in reminder["systemMessage"]
    assert run_hook(state, monkeypatch, capsys, stop) == {}
    ordinary.update(prompt="3", turn_id="dismiss-turn")
    assert run_hook(state, monkeypatch, capsys, ordinary)["continue"] is False
    assert state.note(identifier)["status"] == "dismissed"
    stop["turn_id"] = "later-turn"
    assert run_hook(state, monkeypatch, capsys, stop) == {}
    assert state.session("s").attempts == 1


def test_midturn_notification_has_no_action_until_stop(state, finding, monkeypatch, capsys):
    state.save("s", finding, 100, notify=True)
    event = {"session_id": "s", "hook_event_name": "PostToolUse"}
    alert = run_hook(state, monkeypatch, capsys, event, "notify")
    assert finding.summary in alert["systemMessage"]
    assert "reply" not in alert["systemMessage"]
    event.update(hook_event_name="UserPromptSubmit", prompt="dismiss")
    assert run_hook(state, monkeypatch, capsys, event) == {}


@pytest.mark.parametrize("turn_id", ["continuing-turn", None])
def test_stop_continuation_or_missing_turn_id_cannot_intercept_steering(
    state, finding, monkeypatch, capsys, turn_id
):
    identifier = state.save("s", finding, 100, notify=True)
    state.deliver("s", ready=True, turn_id="continuing-turn")
    event = {
        "session_id": "s",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "dismiss",
        "turn_id": turn_id,
    }
    assert run_hook(state, monkeypatch, capsys, event) == {}
    assert state.note(identifier)["status"] == "new"


def test_async_review_errors_are_delivered_once_by_synchronous_hook(
    state, transcript, monkeypatch, capsys
):
    def reviewer(*args, **kwargs):
        raise backend.BackendError("Review timed out")

    monkeypatch.setattr(backend, "review", reviewer)
    (state.directory / "config.toml").write_text("every_n_tools = 1\n")
    event = {
        "session_id": "s",
        "hook_event_name": "PostToolUse",
        "transcript_path": str(transcript),
    }
    assert run_hook(state, monkeypatch, capsys, event) == {}
    event["hook_event_name"] = "Stop"
    output = run_hook(state, monkeypatch, capsys, event)
    assert "Review timed out" in output["systemMessage"]
    assert "reply" not in output["systemMessage"]
    assert run_hook(state, monkeypatch, capsys, event) == {}


@pytest.mark.parametrize("raw", ["[]", "{}", '{"session_id":4}', "broken"])
def test_bad_hook_input_is_advisory_and_not_a_turn_block(state, monkeypatch, capsys, raw):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    assert main(["--data-dir", str(state.directory), "hook"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"systemMessage"}


def test_recursion_guard_does_not_read_stdin_or_create_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("YSK_REVIEWER_ACTIVE", "1")
    directory = tmp_path / "unused"
    assert main(["--data-dir", str(directory), "hook"]) == 0
    assert capsys.readouterr().out.strip() == "{}"
    assert not directory.exists()


def test_manual_failure_has_nonzero_exit(state, tmp_path, capsys):
    assert (
        main(
            [
                "--data-dir",
                str(state.directory),
                "check",
                "--transcript",
                str(tmp_path / "missing"),
                "--session",
                "s",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_data_paths_and_permissions(state, monkeypatch, tmp_path):
    monkeypatch.setenv("YSK_DATA_DIR", str(tmp_path / "override"))
    assert data_directory() == tmp_path / "override"
    assert data_directory(str(tmp_path / "explicit")) == tmp_path / "explicit"
    assert os.stat(state.directory).st_mode & 0o777 == 0o700
    assert os.stat(state.directory / "state.sqlite3").st_mode & 0o777 == 0o600


@pytest.mark.parametrize("source", ["argument", "environment"])
@pytest.mark.parametrize("command", ["status", "hook", "notify"])
@pytest.mark.parametrize("failure", [RuntimeError, OSError])
def test_data_directory_resolution_failure_uses_normal_error_response(
    tmp_path, monkeypatch, capsys, source, command, failure
):
    directory = tmp_path / "invalid"

    def unavailable(path):
        assert path == directory
        raise failure("synthetic data path failure")

    monkeypatch.setattr(Path, "resolve", unavailable)
    arguments = []
    if source == "argument":
        arguments = ["--data-dir", str(directory)]
    else:
        monkeypatch.setenv("YSK_DATA_DIR", str(directory))
    monkeypatch.delenv("YSK_REVIEWER_ACTIVE", raising=False)
    event = {"session_id": "synthetic", "hook_event_name": "PostToolUse"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))

    assert main([*arguments, command]) == (1 if command == "status" else 0)
    output = capsys.readouterr()
    if command == "status":
        assert not output.out
        assert output.err.startswith("you-should-know: ")
    else:
        assert not output.err
        assert json.loads(output.out)["systemMessage"].startswith("You should know: ")
    assert "synthetic data path failure" in output.out + output.err
    assert "Traceback" not in output.err
    assert not list(tmp_path.iterdir())


def test_data_directory_symlink_loop_is_reported_without_traceback(tmp_path, capsys):
    directory = tmp_path / "loop"
    directory.symlink_to(directory, target_is_directory=True)
    assert main(["--data-dir", str(directory), "status"]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert output.err.startswith("you-should-know: ")
    assert str(directory) in output.err
    assert "Traceback" not in output.err
    assert list(tmp_path.iterdir()) == [directory]


def test_shortcuts_does_not_resolve_unused_data_directory(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "loop"
    directory.symlink_to(directory, target_is_directory=True)
    monkeypatch.setenv("YSK_DATA_DIR", str(directory))

    def unexpected_resolution(path):
        pytest.fail("Shortcut commands must not resolve the unused data directory")

    monkeypatch.setattr(Path, "resolve", unexpected_resolution)
    config = tmp_path / "config.toml"
    original = b'model = "unchanged"\n'
    config.write_bytes(original)

    assert main(["shortcuts", "remove", "--config", str(config)]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "unchanged"
    assert not output.err
    assert config.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml", "loop"]


@pytest.mark.parametrize(
    "text",
    [
        "enabled = 1",
        "every_n_tools = true",
        "unknown = 4",
        "timeout_seconds = 120",
        "model = ''",
        "broken = [",
    ],
)
def test_invalid_settings_are_actionable(tmp_path, text):
    (tmp_path / "config.toml").write_text(text)
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_config_overrides_and_default(tmp_path):
    assert load_config(tmp_path) == Config()
    (tmp_path / "config.toml").write_text('model = "custom"\nenabled = false\n')
    assert load_config(tmp_path) == replace(Config(), model="custom", enabled=False)
