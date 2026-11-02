import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from you_should_know.assessment import Finding
from you_should_know.state import State


@pytest.fixture
def state(tmp_path):
    store = State(tmp_path / "state")
    yield store
    store.close()


def finding(topic="settings lost"):
    return Finding(topic, "Previous settings were deleted.", "Restore the backup.", ["Deleted."])


def test_queued_note_survives_reopen_and_is_delivered_once(tmp_path):
    directory = tmp_path / "state"
    first = State(directory)
    identifier = first.save("session", finding(), 100, notify=True)
    assert identifier is not None
    first.close()
    reopened = State(directory)
    try:
        expected = reopened.note(identifier)
        assert reopened.deliver("session", ready=True, turn_id="finished-turn") == (
            [{"note": expected}],
            {**expected, "expanded": False},
        )
        assert reopened.deliver("session", ready=True, turn_id="finished-turn") == ([], None)
        assert reopened.reply("session", "more", turn_id="reply-turn") == {
            **expected,
            "expanded": True,
        }
    finally:
        reopened.close()


def test_manual_notes_and_duplicates_are_not_queued(state):
    manual = state.save("session", finding("manual"), 100)
    automatic = state.save("session", finding("automatic"), 100, notify=True)
    assert state.save("other", finding("manual"), 200, notify=True) is None
    assert state.save("other", finding("automatic"), 200, notify=True) is None
    assert state.deliver("session") == ([{"note": state.note(automatic)}], None)
    assert state.deliver("other", ready=True, turn_id="finished-turn") == ([], None)
    assert state.note(manual)["status"] == "new"


def test_delivery_preserves_enqueue_order_and_arms_latest_note(state):
    state.queue_error("session", "first error")
    first = state.save("session", finding("first"), 300, notify=True)
    state.queue_error("session", "second error")
    last = state.save("session", finding("last"), 100, notify=True)
    expected = [
        {"message": "first error"},
        {"note": state.note(first)},
        {"message": "second error"},
        {"note": state.note(last)},
    ]
    assert state.deliver("session", ready=True, turn_id="finished-turn") == (
        expected,
        {**state.note(last), "expanded": False},
    )
    assert state.reply("session", "more", turn_id="reply-turn") == {
        **state.note(last),
        "expanded": True,
    }


def test_delivery_and_replies_are_isolated_by_session(state):
    first = state.save("one", finding("first"), 100, notify=True)
    second = state.save("two", finding("second"), 200, notify=True)
    state.queue_error("two", "second session error")
    assert state.deliver("one", ready=True, turn_id="finished-turn") == (
        [{"note": state.note(first)}],
        {**state.note(first), "expanded": False},
    )
    assert state.reply("two", "known", turn_id="reply-turn") is None
    assert state.reply("one", "known", turn_id="reply-turn")["id"] == first
    assert state.deliver("two", ready=True, turn_id="finished-turn") == (
        [{"note": state.note(second)}, {"message": "second session error"}],
        {**state.note(second), "expanded": False},
    )
    assert state.reply("two", "more", turn_id="reply-turn")["id"] == second
    assert state.note(second)["status"] == "new"


def test_stop_arms_midturn_notification_with_summary(state):
    identifier = state.save("session", finding(), 100, notify=True)
    expected = state.note(identifier)
    assert state.deliver("session") == ([{"note": expected}], None)
    assert state.deliver("session") == ([], None)
    assert state.deliver("session", ready=True, turn_id="finished-turn") == (
        [],
        {**expected, "expanded": False},
    )
    assert state.deliver("session", ready=True, turn_id="finished-turn") == ([], None)
    assert state.reply("session", "more", turn_id="reply-turn") == {**expected, "expanded": True}


@pytest.mark.parametrize("action", ["more", "known", "dismissed", "chat"])
def test_midturn_reply_disarms_actions_but_keeps_note_for_stop(state, action):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session")
    assert state.reply("session", action, turn_id="reply-turn") is None
    assert state.deliver("session", ready=True, turn_id="finished-turn") == (
        [],
        {**state.note(identifier), "expanded": False},
    )
    assert state.note(identifier)["status"] == "new"


def test_ordinary_prompt_disarms_actions_until_later_stop_reminds(state):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    assert state.reply("session", None, turn_id="reply-turn") is None
    assert state.reply("session", "known", turn_id="reply-turn") is None
    assert state.note(identifier)["status"] == "new"
    expected = {**state.note(identifier), "expanded": False}
    assert state.deliver("session", ready=True, turn_id="reply-turn") == (
        [{"note": expected, "reminder": True}],
        expected,
    )
    assert state.reply("session", "known", turn_id="action-turn")["status"] == "known"
    assert state.deliver("session", ready=True, turn_id="later-turn") == ([], None)


@pytest.mark.parametrize("action", ["more", "known", "dismissed", "chat"])
def test_same_turn_steering_disarms_until_stop_rearms_without_repeating_note(state, action):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="active-turn")
    assert state.reply("session", action, turn_id="active-turn") is None
    assert state.reply("session", action, turn_id="later-turn") is None
    assert state.note(identifier)["status"] == "new"
    assert state.deliver("session", ready=True, turn_id="active-turn") == ([], None)
    assert state.reply("session", "more", turn_id="later-turn") == {
        **state.note(identifier),
        "expanded": True,
    }


def test_unresolved_note_is_reminded_once_on_each_distinct_turn(state):
    identifier = state.save("session", finding(), 100, notify=True)
    expected = state.note(identifier)
    assert state.deliver("session", ready=True, turn_id="first-turn") == (
        [{"note": expected}],
        {**expected, "expanded": False},
    )
    expected["expanded"] = False
    for turn_id in ("second-turn", "third-turn"):
        assert state.deliver("session", ready=True, turn_id=turn_id) == (
            [{"note": expected, "reminder": True}],
            expected,
        )
        assert state.deliver("session", ready=True, turn_id=turn_id) == ([], None)


def test_delivered_reminder_survives_reopen_after_an_ordinary_prompt(tmp_path):
    directory = tmp_path / "state"
    first = State(directory)
    identifier = first.save("session", finding(), 100, notify=True)
    assert identifier is not None
    first.deliver("session", ready=True, turn_id="first-turn")
    first.reply("session", None, turn_id="second-turn")
    expected = {**first.note(identifier), "expanded": False}
    first.close()
    reopened = State(directory)
    try:
        assert reopened.reply("session", "known", turn_id="second-turn") is None
        assert reopened.deliver("session", ready=True, turn_id="second-turn") == (
            [{"note": expected, "reminder": True}],
            expected,
        )
        assert reopened.reply("session", "more", turn_id="reply-turn") == {
            **expected,
            "expanded": True,
        }
    finally:
        reopened.close()


@pytest.mark.parametrize("turn_id", [None, ""])
def test_missing_stop_turn_id_delivers_note_without_arming(state, turn_id):
    identifier = state.save("session", finding(), 100, notify=True)
    expected = state.note(identifier)
    assert state.deliver("session", ready=True, turn_id=turn_id) == ([{"note": expected}], None)
    assert state.reply("session", "known", turn_id="reply-turn") is None
    assert state.note(identifier)["status"] == "new"


@pytest.mark.parametrize("turn_id", [None, ""])
def test_missing_reply_turn_id_clears_target_without_consuming_action(state, turn_id):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    assert state.reply("session", "known", turn_id=turn_id) is None
    assert state.reply("session", "known", turn_id="reply-turn") is None
    assert state.note(identifier)["status"] == "new"


def test_interaction_without_stored_turn_id_is_migrated_and_cannot_consume_reply(state):
    identifier = state.save("session", finding(), 100)
    with state.connection:
        state.connection.execute("DROP TABLE reminders")
        state.connection.execute("DROP TABLE interactions")
        state.connection.execute(
            "CREATE TABLE interactions (session_id TEXT PRIMARY KEY, "
            "note_id TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0)"
        )
        state.connection.execute("INSERT INTO interactions VALUES ('session', ?, 1)", (identifier,))
    reopened = State(state.directory)
    try:
        assert reopened.reply("session", "known", turn_id="reply-turn") is None
        assert reopened.note(identifier)["status"] == "new"
        assert reopened.deliver("session", ready=True, turn_id="finished-turn") == (
            [],
            {**reopened.note(identifier), "expanded": False},
        )
    finally:
        reopened.close()


@pytest.mark.parametrize("ready", [False, True])
def test_migration_keeps_current_interaction_without_resurfacing_other_notes(state, ready):
    historical = state.save("session", finding("historical"), 100, notify=True)
    state.deliver("session", ready=True, turn_id="historical-turn")
    shared = state.save("session", finding("shared"), 101, notify=True)
    state.deliver("session", ready=True, turn_id="shared-turn")
    state.reply("session", "more", turn_id="details-turn")
    state.reply("session", "chat", turn_id="chat-turn")
    manual = state.save("session", finding("manual"), 102)
    active = state.save("session", finding("active"), 103, notify=True)
    state.deliver("session", ready=ready, turn_id="legacy-turn")
    with state.connection:
        state.connection.execute("DROP TABLE reminders")
    migrated = State(state.directory)
    try:
        expected = {**migrated.note(active), "expanded": False}
        assert migrated.deliver("session", ready=True, turn_id="legacy-turn") == (
            [],
            None if ready else expected,
        )
        assert migrated.deliver("session", ready=True, turn_id="new-turn") == (
            [{"note": expected, "reminder": True}],
            expected,
        )
        expected["expanded"] = True
        assert migrated.reply("session", "more", turn_id="details-turn") == expected
        assert migrated.reply("session", "chat", turn_id="reply-turn") == expected
        assert migrated.deliver("session", ready=True, turn_id="later-turn") == ([], None)
        assert all(
            migrated.note(note_id)["status"] == "new" for note_id in (historical, shared, manual)
        )
    finally:
        migrated.close()
    reopened = State(state.directory)
    try:
        assert reopened.deliver("session", ready=True, turn_id="resumed-turn") == ([], None)
    finally:
        reopened.close()


def test_more_keeps_target_for_later_disposition(state):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    expected = {**state.note(identifier), "expanded": True}
    assert state.reply("session", "more", turn_id="reply-turn") == expected
    assert state.deliver("session", ready=True, turn_id="later-turn") == (
        [{"note": expected, "reminder": True}],
        expected,
    )
    assert state.reply("session", "dismissed", turn_id="action-turn")["status"] == "dismissed"
    assert state.reply("session", "more", turn_id="reply-turn") is None
    assert state.deliver("session", ready=True, turn_id="later-turn") == ([], None)


def test_chat_returns_note_and_ends_interaction_without_disposition(state):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    expected = {**state.note(identifier), "expanded": True}
    assert state.reply("session", "more", turn_id="details-turn") == expected
    assert state.reply("session", "chat", turn_id="chat-turn") == expected
    assert state.note(identifier)["status"] == "new"
    assert state.reply("session", "chat", turn_id="later-turn") is None
    assert state.reply("session", "dismissed", turn_id="later-turn") is None
    assert state.deliver("session", ready=True, turn_id="later-turn") == ([], None)


def test_chat_before_details_reaches_agent_without_consuming_note(state):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    assert state.reply("session", "chat", turn_id="reply-turn") is None
    assert state.reply("session", "more", turn_id="reply-turn") is None
    assert state.note(identifier)["status"] == "new"
    expected = {**state.note(identifier), "expanded": False}
    assert state.deliver("session", ready=True, turn_id="reply-turn") == (
        [{"note": expected, "reminder": True}],
        expected,
    )
    details = state.reply("session", "more", turn_id="details-turn")
    assert details == {**state.note(identifier), "expanded": True}
    assert state.reply("session", "chat", turn_id="chat-turn") == details
    assert state.deliver("session", ready=True, turn_id="later-turn") == ([], None)


@pytest.mark.parametrize("action", ["more", "known"])
def test_unavailable_details_actions_disarm_without_losing_expanded_reminder(state, action):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    details = state.reply("session", "more", turn_id="details-turn")
    assert state.reply("session", action, turn_id="action-turn") is None
    assert state.reply("session", "chat", turn_id="action-turn") is None
    assert state.note(identifier)["status"] == "new"
    assert state.deliver("session", ready=True, turn_id="action-turn") == (
        [{"note": details, "reminder": True}],
        details,
    )
    assert state.reply("session", "chat", turn_id="chat-turn") == details


def test_expanded_reminder_survives_ordinary_prompt_and_process_restart(tmp_path):
    directory = tmp_path / "state"
    first = State(directory)
    identifier = first.save("session", finding(), 100, notify=True)
    assert identifier is not None
    first.deliver("session", ready=True, turn_id="first-turn")
    expected = {**first.note(identifier), "expanded": True}
    assert first.reply("session", "more", turn_id="details-turn") == expected
    assert first.reply("session", None, turn_id="work-turn") is None
    first.close()
    reopened = State(directory)
    try:
        assert reopened.reply("session", "chat", turn_id="work-turn") is None
        assert reopened.deliver("session", ready=True, turn_id="work-turn") == (
            [{"note": expected, "reminder": True}],
            expected,
        )
        assert reopened.reply("session", "chat", turn_id="chat-turn") == expected
        assert reopened.note(identifier)["status"] == "new"
        assert reopened.deliver("session", ready=True, turn_id="later-turn") == ([], None)
    finally:
        reopened.close()


def test_new_note_starts_unexpanded_without_resetting_older_note_details(state):
    first = state.save("session", finding("first"), 100, notify=True)
    state.deliver("session", ready=True, turn_id="first-turn")
    details = state.reply("session", "more", turn_id="details-turn")
    second = state.save("session", finding("second"), 200, notify=True)
    notifications, active = state.deliver("session", ready=True, turn_id="second-turn")
    assert notifications == [{"note": state.note(second)}]
    assert active == {**state.note(second), "expanded": False}
    assert state.reply("session", "known", turn_id="known-turn")["id"] == second
    assert state.deliver("session", ready=True, turn_id="third-turn") == (
        [{"note": details, "reminder": True}],
        details,
    )
    assert state.reply("session", "chat", turn_id="chat-turn") == details
    assert state.note(first)["status"] == "new"


def test_reminder_migration_preserves_delivery_order_and_defaults_to_summary(state):
    first = state.save("session", finding("first"), 300, notify=True)
    state.deliver("session", ready=True, turn_id="first-turn")
    second = state.save("session", finding("second"), 100, notify=True)
    state.deliver("session", ready=True, turn_id="second-turn")
    with state.connection:
        state.connection.execute("ALTER TABLE reminders RENAME TO reminders_current")
        state.connection.execute(
            "CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT NOT NULL, note_id TEXT NOT NULL UNIQUE, "
            "last_turn_id TEXT NOT NULL DEFAULT '')"
        )
        state.connection.execute(
            "INSERT INTO reminders SELECT id, session_id, note_id, last_turn_id "
            "FROM reminders_current"
        )
        state.connection.execute("DROP TABLE reminders_current")
        state.connection.execute("CREATE INDEX reminders_session ON reminders(session_id, id)")
    migrated = State(state.directory)
    try:
        assert migrated.deliver("session", ready=True, turn_id="second-turn") == ([], None)
        assert migrated.reply("session", "chat", turn_id="reply-turn") is None
        expected = {**migrated.note(second), "expanded": False}
        assert migrated.deliver("session", ready=True, turn_id="reply-turn") == (
            [{"note": expected, "reminder": True}],
            expected,
        )
        details = migrated.reply("session", "more", turn_id="details-turn")
        assert details == {**expected, "expanded": True}
        assert migrated.reply("session", "chat", turn_id="chat-turn") == details
        expected = {**migrated.note(first), "expanded": False}
        assert migrated.deliver("session", ready=True, turn_id="later-turn") == (
            [{"note": expected, "reminder": True}],
            expected,
        )
        migrated.reply("session", "more", turn_id="expanded-turn")
    finally:
        migrated.close()
    reopened = State(state.directory)
    try:
        assert reopened.reply("session", "chat", turn_id="chat-turn") == {
            **expected,
            "expanded": True,
        }
        assert reopened.deliver("session", ready=True, turn_id="final-turn") == ([], None)
    finally:
        reopened.close()


def test_failed_details_expansion_leaves_summary_actions_available(state):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="first-turn")
    state.connection.execute(
        "CREATE TRIGGER fail_expansion BEFORE UPDATE OF expanded ON reminders "
        "BEGIN SELECT RAISE(ABORT, 'expansion unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="expansion unavailable"):
        state.reply("session", "more", turn_id="details-turn")
    assert state.reply("session", "known", turn_id="known-turn") == {
        **state.note(identifier),
        "expanded": False,
    }
    assert state.note(identifier)["status"] == "known"


@pytest.mark.parametrize("action", ["known", "dismissed"])
def test_disposition_updates_note_and_clears_target(state, action):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    result = state.reply("session", action, turn_id="reply-turn")
    assert result == {**state.note(identifier), "expanded": False}
    assert result["status"] == action
    assert state.notes() == []
    assert state.reply("session", "more", turn_id="reply-turn") is None
    assert state.deliver("session", ready=True, turn_id="later-turn") == ([], None)


@pytest.mark.parametrize("status", ["known", "dismissed"])
def test_resolved_pending_notes_are_skipped(state, status):
    identifier = state.save("session", finding(), 100, notify=True)
    state.mark(identifier, status)
    state.queue_error("session", "review failed")
    assert state.deliver("session", ready=True, turn_id="finished-turn") == (
        [{"message": "review failed"}],
        None,
    )
    assert state.reply("session", "more", turn_id="reply-turn") is None
    assert state.deliver("session", ready=True, turn_id="finished-turn") == ([], None)


@pytest.mark.parametrize("status", ["known", "dismissed"])
def test_explicit_mark_invalidates_displayed_interaction_and_reminder(state, status):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    state.mark(identifier, status)
    assert state.reply("session", "dismissed", turn_id="reply-turn") is None
    assert state.note(identifier)["status"] == status
    assert state.deliver("session", ready=True, turn_id="later-turn") == ([], None)


@pytest.mark.parametrize("action", ["known", "dismissed", "chat"])
def test_new_note_takes_precedence_without_losing_older_reminder(state, action):
    first = state.save("session", finding("first"), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    second = state.save("session", finding("second"), 200, notify=True)
    assert state.deliver("session") == ([{"note": state.note(second)}], None)
    assert state.reply("session", "known", turn_id="reply-turn") is None
    assert state.note(first)["status"] == "new"
    assert state.note(second)["status"] == "new"
    assert state.deliver("session", ready=True, turn_id="second-turn") == (
        [],
        {**state.note(second), "expanded": False},
    )
    if action == "chat":
        assert state.reply("session", "more", turn_id="details-turn")["id"] == second
    assert state.reply("session", action, turn_id="action-turn")["id"] == second
    assert state.reply("session", "known", turn_id="action-turn") is None
    expected = {**state.note(first), "expanded": False}
    assert state.deliver("session", ready=True, turn_id="third-turn") == (
        [{"note": expected, "reminder": True}],
        expected,
    )
    assert state.reply("session", "more", turn_id="reply-turn") == {**expected, "expanded": True}


def test_unknown_actions_are_rejected_without_losing_target(state):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="finished-turn")
    with pytest.raises(ValueError, match="Invalid note action"):
        state.reply("session", "delete", turn_id="reply-turn")
    assert state.reply("session", "more", turn_id="reply-turn") == {
        **state.note(identifier),
        "expanded": True,
    }


def test_note_and_notification_are_saved_atomically(state):
    state.connection.execute(
        "CREATE TRIGGER fail_notification BEFORE INSERT ON notifications "
        "BEGIN SELECT RAISE(ABORT, 'queue unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="queue unavailable"):
        state.save("session", finding(), 100, notify=True)
    assert state.notes(True) == []
    assert state.deliver("session", ready=True, turn_id="finished-turn") == ([], None)


def test_failed_reminder_insert_leaves_notification_available_for_retry(state):
    identifier = state.save("session", finding(), 100, notify=True)
    expected = state.note(identifier)
    state.connection.execute(
        "CREATE TRIGGER fail_reminder BEFORE INSERT ON reminders "
        "BEGIN SELECT RAISE(ABORT, 'reminder unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="reminder unavailable"):
        state.deliver("session", ready=True, turn_id="finished-turn")
    state.connection.execute("DROP TRIGGER fail_reminder")
    assert state.deliver("session", ready=True, turn_id="finished-turn") == (
        [{"note": expected}],
        {**expected, "expanded": False},
    )


def test_failed_disposition_keeps_note_and_reminder_actionable(state):
    identifier = state.save("session", finding(), 100, notify=True)
    state.deliver("session", ready=True, turn_id="first-turn")
    state.connection.execute(
        "CREATE TRIGGER fail_reminder_delete BEFORE DELETE ON reminders "
        "BEGIN SELECT RAISE(ABORT, 'reminder unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="reminder unavailable"):
        state.reply("session", "known", turn_id="reply-turn")
    assert state.note(identifier)["status"] == "new"
    expected = {**state.note(identifier), "expanded": True}
    assert state.reply("session", "more", turn_id="reply-turn") == expected
    assert state.deliver("session", ready=True, turn_id="second-turn") == (
        [{"note": expected, "reminder": True}],
        expected,
    )


def test_old_database_is_preserved_without_replaying_existing_notes(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    connection = sqlite3.connect(directory / "state.sqlite3")
    connection.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            events INTEGER NOT NULL DEFAULT 0,
            checked_events INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt REAL NOT NULL DEFAULT 0,
            last_success REAL NOT NULL DEFAULT 0,
            last_hash TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE notes (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            summary TEXT NOT NULL,
            explanation TEXT NOT NULL,
            evidence TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at REAL NOT NULL
        );
        INSERT INTO sessions(session_id, events, attempts) VALUES ('session', 42, 7);
    """)
    with connection:
        connection.execute(
            "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old-note",
                "session",
                "old topic",
                "Old summary",
                "Old explanation",
                '["é"]',
                "new",
                1,
            ),
        )
    connection.close()
    migrated = State(directory)
    try:
        assert migrated.session("session").events == 42
        assert migrated.session("session").attempts == 7
        assert migrated.note("old-note") == {
            "id": "old-note",
            "session_id": "session",
            "topic": "old topic",
            "summary": "Old summary",
            "explanation": "Old explanation",
            "evidence": ["é"],
            "status": "new",
            "created_at": 1,
        }
        assert migrated.deliver("session", ready=True, turn_id="finished-turn") == ([], None)
        assert migrated.reply("session", "known", turn_id="reply-turn") is None
        identifier = migrated.save("session", finding(), 100, notify=True)
        assert identifier is not None
        assert migrated.deliver("session", ready=True, turn_id="finished-turn") == (
            [{"note": migrated.note(identifier)}],
            {**migrated.note(identifier), "expanded": False},
        )
        raw = migrated.connection.execute(
            "SELECT evidence FROM notes WHERE id = 'old-note'"
        ).fetchone()[0]
        assert raw == '["é"]'
        assert json.loads(raw) == ["é"]
    finally:
        migrated.close()


def test_concurrent_delivery_and_disposition_claim_each_item_once(state):
    identifier = state.save("session", finding(), 100, notify=True)
    barrier = threading.Barrier(2)

    def concurrent(operation):
        store = State(state.directory)
        try:
            barrier.wait(timeout=5)
            return operation(store)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        deliveries = list(
            pool.map(
                concurrent,
                [lambda store: store.deliver("session", ready=True, turn_id="finished-turn")] * 2,
            )
        )
        assert sum(len(items) for items, _ in deliveries) == 1
        assert sum(note is not None for _, note in deliveries) == 1
        actions = list(
            pool.map(
                concurrent,
                [lambda store: store.reply("session", "known", turn_id="reply-turn")] * 2,
            )
        )
    assert [note["id"] for note in actions if note is not None] == [identifier]
    assert state.note(identifier)["status"] == "known"
