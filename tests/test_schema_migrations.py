"""Preserve legacy data while sharing schema reads during state initialization."""

import sqlite3
from collections import Counter

import pytest

from you_should_know.assessment import Finding
from you_should_know.state import State

# Expected backfills for historical fields removed from the populated fixture.
BACKFILLS = {
    "sessions": {
        "touched_at": 200.0,
        "cwd": "",
        "consecutive_transcript_failures": 0,
        "unknown_record_types": "{}",
    },
    "notes": {"touched_at": 100.0},
    "interactions": {"ready_at": 0.0},
    "notifications": {"created_at": 1000.0},
    "candidates": {"assistant_claim": 0, "checkpoint": "{}"},
    "checks": {
        "model_called": 0,
        "usage_reported": 0,
        "model": None,
        "model_source": None,
        "diagnostics": "{}",
        "candidate_id": None,
        "note_id": None,
    },
}


def rows(connection):
    return {
        table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in BACKFILLS
    }


@pytest.fixture
def populated(tmp_path, monkeypatch):
    monkeypatch.setattr("you_should_know.state.time.time", lambda: 1000.0)
    state = State(tmp_path)
    finding = Finding("Topic", "A consequence.", "An explanation.", ["verbatim evidence"])
    state.observe("session", True, cwd=str(tmp_path))
    candidate = state.candidate(
        "session", finding, 100, "transcript", 123, assistant_claim=True, checkpoint={"sha": "abc"}
    )
    note = state.save("session", finding, 100, notify=True)
    state.record_check(
        "session",
        "Stop",
        "finding",
        101,
        0.5,
        usage={"input_tokens": 20, "output_tokens": 3},
        model="model",
        model_source="config",
        diagnostics={"stage": "done"},
        candidate_id=candidate,
        note_id=note,
    )
    with state.connection:
        state.connection.execute(
            "UPDATE sessions SET last_attempt = 200, touched_at = 800, "
            "consecutive_transcript_failures = 2, unknown_record_types = '{\"custom\": 1}'"
        )
        state.connection.execute("UPDATE notes SET touched_at = 900")
        state.connection.execute(
            "INSERT INTO interactions(session_id, note_id, ready, turn_id, ready_at) "
            "VALUES ('session', ?, 1, 'finished', 950)",
            (note,),
        )
    yield state
    state.close()


MISSING_FIELDS = [(table, column) for table, columns in BACKFILLS.items() for column in columns]


@pytest.mark.parametrize("missing", [[field] for field in MISSING_FIELDS] + [MISSING_FIELDS])
def test_missing_fields_backfill_without_overwriting_existing_neighbors(populated, missing):
    expected = rows(populated.connection)
    with populated.connection:
        for table, column in missing:
            populated.connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            expected[table][0][column] = BACKFILLS[table][column]
            if table == "interactions":
                expected[table][0]["ready"] = 0
    for _ in range(2):
        reopened = State(populated.directory)
        try:
            assert rows(reopened.connection) == expected
        finally:
            reopened.close()


@pytest.mark.parametrize(
    ("attempt", "success", "expected"), [(0, 0, 1000), (100, 200, 200), (200, 100, 200)]
)
def test_session_backfill_uses_latest_activity_or_now(populated, attempt, success, expected):
    with populated.connection:
        populated.connection.execute(
            "UPDATE sessions SET last_attempt = ?, last_success = ?", (attempt, success)
        )
        populated.connection.execute("ALTER TABLE sessions DROP COLUMN touched_at")
    reopened = State(populated.directory)
    try:
        assert reopened.session("session").touched_at == expected
    finally:
        reopened.close()


def test_schema_cache_does_not_cross_directories_or_reopens(populated, tmp_path):
    other = State(tmp_path / "other")
    other.close()
    with populated.connection:
        populated.connection.execute("ALTER TABLE checks DROP COLUMN model")
        populated.connection.execute("ALTER TABLE checks DROP COLUMN model_source")
    reopened = State(populated.directory)
    try:
        assert reopened.diagnostics()["recent_checks"][0]["model"] is None
        assert reopened.diagnostics()["recent_checks"][0]["model_source"] is None
    finally:
        reopened.close()


def test_later_backfill_failure_rolls_back_earlier_migrations(populated, monkeypatch):
    with populated.connection:
        populated.connection.execute("ALTER TABLE sessions DROP COLUMN cwd")
        populated.connection.execute("ALTER TABLE notifications DROP COLUMN created_at")
        populated.connection.execute(
            "CREATE TRIGGER fail_backfill BEFORE UPDATE ON notifications "
            "BEGIN SELECT RAISE(ABORT, 'backfill unavailable'); END"
        )
    connections = []
    connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", tracked_connect)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="backfill unavailable"):
                State(populated.directory)
        finally:
            for connection in connections:
                connection.close()
    assert "cwd" not in rows(populated.connection)["sessions"][0]
    assert "created_at" not in rows(populated.connection)["notifications"][0]
    with populated.connection:
        populated.connection.execute("DROP TRIGGER fail_backfill")
    recovered = State(populated.directory)
    try:
        assert recovered.session("session").cwd == ""
        assert rows(recovered.connection)["notifications"][0]["created_at"] == 1000
    finally:
        recovered.close()


def test_current_database_inspects_each_migration_table_once(populated, monkeypatch):
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    reopened = State(populated.directory)
    reopened.close()
    counts = Counter(
        statement for statement in statements if statement.startswith("PRAGMA table_info")
    )
    assert counts == {
        "PRAGMA table_info(sessions)": 1,
        "PRAGMA table_info(notes)": 1,
        # The earlier reminder migration has its own interaction-schema check.
        "PRAGMA table_info(interactions)": 2,
        "PRAGMA table_info(reminders)": 1,
        "PRAGMA table_info(notifications)": 1,
        "PRAGMA table_info(candidates)": 1,
        "PRAGMA table_info(checks)": 1,
    }
    assert not any(statement.startswith(("ALTER TABLE", "UPDATE ")) for statement in statements)
