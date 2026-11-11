"""Retention cutoffs and database rollback use only temporary local state."""

import hashlib
import os
import sqlite3
from contextlib import closing

import pytest

from you_should_know.assessment import Finding
from you_should_know.state import State, review_lock

DAY = 86_400
NOW = 40 * DAY
CUTOFF = NOW - 30 * DAY
TABLES = (
    "sessions",
    "notes",
    "candidates",
    "mutes",
    "checks",
    "attempts",
    "notices",
    "notifications",
    "hook_events",
    "transcript_retries",
    "reviewer_backoff",
    "reminders",
    "interactions",
)


def finding(topic):
    return Finding(topic, "Synthetic summary", "Synthetic explanation", ["Synthetic evidence"])


@pytest.fixture
def state(tmp_path):
    state = State(tmp_path)
    try:
        yield state
    finally:
        state.close()


@pytest.mark.parametrize(
    "table",
    [
        "sessions",
        "notes",
        "candidates",
        "mutes",
        "checks",
        "attempts",
        "notices",
        "notifications",
        "hook_events",
    ],
)
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["before", "at", "after"])
def test_retention_keeps_rows_at_the_cutoff(state, monkeypatch, table, offset):
    timestamp = CUTOFF + offset
    monkeypatch.setattr("you_should_know.state.time.time", lambda: timestamp)
    if table == "sessions":
        state.observe("active", False)
    elif table == "notes":
        # Retention follows the last touch, not the original creation time.
        assert state.save("active", finding("note"), 1) is not None
        assert state.save("active", finding("note"), timestamp) is None
    elif table == "candidates":
        identifier = state.candidate("active", finding("candidate"), 1, "source", 0)
        assert state.resolve_candidate(identifier, "pending", "deferred", timestamp)
    elif table == "mutes":
        state.mute("active")
    elif table == "checks":
        state.record_check("active", "Stop", "clear", timestamp, 0)
    elif table == "attempts":
        state.begin("active", timestamp)
    elif table == "notices":
        assert state.notice_once("active", "synthetic notice")
    elif table == "notifications":
        state.queue_error("active", "synthetic error")
    elif table == "hook_events":
        state.hook_seen("active", "Stop")

    if table != "sessions":
        # Keep session cleanup from masking the individual record's age rule.
        monkeypatch.setattr("you_should_know.state.time.time", lambda: NOW)
        state.observe("active", False)
    before = [tuple(row) for row in state.connection.execute(f"SELECT * FROM {table}")]
    assert len(before) == 1
    report = state.prune(30, NOW)
    after = [tuple(row) for row in state.connection.execute(f"SELECT * FROM {table}")]
    assert report[table] == (1 if offset < 0 else 0)
    assert after == ([] if offset < 0 else before)


@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["before", "at", "after"])
def test_backoff_expires_at_retry_time_instead_of_retention_cutoff(state, offset):
    state.defer_reviewer("synthetic-model", NOW + offset - 30, 30)
    report = state.prune(30, NOW)
    assert report["reviewer_backoff"] == (1 if offset <= 0 else 0)
    assert state.reviewer_retry_at("synthetic-model") == (0 if offset <= 0 else NOW + offset)


@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["before", "at", "after"])
@pytest.mark.parametrize("active_session", [False, True], ids=["orphan", "active"])
def test_lock_retention_uses_mtime_and_keeps_active_session_locks(
    state, monkeypatch, offset, active_session
):
    monkeypatch.setattr("you_should_know.state.time.time", lambda: NOW)
    if active_session:
        state.observe("lock-session", False)
    with review_lock(state.directory, "lock-session") as acquired:
        assert acquired
    key = hashlib.sha256(b"lock-session").hexdigest()
    path = state.directory / f"review-{key}.lock"
    os.utime(path, (CUTOFF + offset, CUTOFF + offset))
    report = state.prune(30, NOW)
    removed = offset < 0 and not active_session
    assert report["locks"] == int(removed)
    assert path.exists() is not removed


def seed_retained_state(state, session, timestamp, monkeypatch):
    monkeypatch.setattr("you_should_know.state.time.time", lambda: timestamp)
    state.hook_seen(session, "Stop")
    state.mute(session)
    state.save(session, finding(f"note-{session}"), timestamp, notify=True)
    state.deliver(session, ready=True, turn_id=f"turn-{session}")
    state.queue_error(session, f"error-{session}")
    state.candidate(session, finding(f"candidate-{session}"), timestamp, "source", 0)
    state.begin(session, timestamp)
    state.record_check(session, "Stop", "clear", timestamp, 0)
    state.notice_once(session, "synthetic notice")
    state.record_transcript_read(session, failed=True)
    state.defer_reviewer(session, timestamp, 30)
    with review_lock(state.directory, session) as acquired:
        assert acquired
    path = state.directory / f"review-{hashlib.sha256(session.encode()).hexdigest()}.lock"
    os.utime(path, (timestamp, timestamp))
    return path


def snapshot(connection):
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in TABLES
    }


@pytest.mark.parametrize("failed_table", ["candidates", "checks", "notifications"])
def test_prune_failure_rolls_back_all_tables_before_unlinking_locks(
    state, monkeypatch, failed_table
):
    old_lock = seed_retained_state(state, "old", 1, monkeypatch)
    recent_lock = seed_retained_state(state, "recent", NOW, monkeypatch)
    before = snapshot(state.connection)
    assert all(len(rows) == 2 for rows in before.values())
    state.connection.execute(
        f"CREATE TRIGGER fail_prune BEFORE DELETE ON {failed_table} "
        "BEGIN SELECT RAISE(ABORT, 'synthetic prune failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="synthetic prune failure"):
        state.prune(30, NOW)
    assert not state.connection.in_transaction
    assert snapshot(state.connection) == before
    with closing(sqlite3.connect(state.directory / "state.sqlite3")) as reopened:
        assert snapshot(reopened) == before
    assert old_lock.exists() and recent_lock.exists()

    state.connection.execute("DROP TRIGGER fail_prune")
    report = state.prune(30, NOW)
    assert report == {**dict.fromkeys(TABLES, 1), "locks": 1}
    expected = {table: [rows[1]] for table, rows in before.items()}
    assert snapshot(state.connection) == expected
    assert not old_lock.exists() and recent_lock.exists()
    assert state.prune(30, NOW) == dict.fromkeys((*TABLES, "locks"), 0)
