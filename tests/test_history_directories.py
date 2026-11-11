from collections import Counter

import pytest

from you_should_know import state as state_module
from you_should_know.state import State

HISTORIES = ("recent_checks", "recent_transcript_errors", "recent_evidence_rejections")


@pytest.fixture
def state(tmp_path):
    store = State(tmp_path / "state")
    try:
        yield store
    finally:
        store.close()


def add_history(state, session, count=3):
    for index in range(count):
        state.record_check(
            session,
            "Stop",
            "error",
            index,
            1,
            diagnostics={
                "transcript_errors": [{"line_number": index}],
                "evidence_rejection": {"reason": str(index)},
            },
        )


def stored_directory(state, session, cwd):
    state.observe(session, False)
    # Legacy state can retain relative paths or symlink aliases.
    with state.connection:
        state.connection.execute("UPDATE sessions SET cwd = ? WHERE session_id = ?", (cwd, session))


def test_history_preserves_directory_forms_and_row_order(state, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    forms = {
        "absolute": str(project),
        "relative": "project",
        "alias": str(alias),
        "missing": str(tmp_path / "missing"),
        "empty": "",
        "invalid": str(loop),
    }
    for session, cwd in forms.items():
        stored_directory(state, session, cwd)
        add_history(state, session)
    add_history(state, "orphan")
    expected = {
        "absolute": str(project),
        "relative": str(project),
        "alias": str(project),
        "missing": str(tmp_path / "missing"),
        "empty": "",
        "invalid": "",
        "orphan": "",
    }
    report = state.diagnostics(limit=30)
    for history in HISTORIES:
        rows = report[history]
        assert len(rows) == 21
        assert [row["id"] for row in rows] == sorted((row["id"] for row in rows), reverse=True)
        assert {
            session: {row["cwd"] for row in rows if row["session_id"] == session}
            for session in expected
        } == {session: {cwd} for session, cwd in expected.items()}
        assert all(row["candidate"] is None and row["note"] is None for row in rows)


@pytest.mark.parametrize("initial", ["directory", "loop", "missing"])
def test_history_refreshes_filesystem_results_on_each_read(state, tmp_path, initial):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "alias"
    if initial != "missing":
        alias.symlink_to(first if initial == "directory" else alias, target_is_directory=True)
    stored_directory(state, "s", str(alias))
    add_history(state, "s")
    before = {"directory": str(first), "loop": "", "missing": str(alias)}[initial]
    for history in HISTORIES:
        assert [row["cwd"] for row in state.diagnostics()[history]] == [before] * 3
    if initial != "missing":
        alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    for history in HISTORIES:
        assert [row["cwd"] for row in state.diagnostics()[history]] == [str(second)] * 3
    stored_directory(state, "s", str(first))
    for history in HISTORIES:
        assert [row["cwd"] for row in state.diagnostics()[history]] == [str(first)] * 3


@pytest.mark.parametrize("limit", [1, 20, 1000])
def test_history_resolves_each_distinct_directory_once_per_list(
    state, tmp_path, monkeypatch, limit
):
    paths = [str(tmp_path / "first"), str(tmp_path / "second")]
    for session, cwd in (("a", paths[0]), ("b", paths[0]), ("c", paths[1])):
        stored_directory(state, session, cwd)
        add_history(state, session, 10)
    calls = Counter()
    original = state_module._stored_cwd

    def counted(cwd):
        calls[cwd] += 1
        return original(cwd)

    monkeypatch.setattr(state_module, "_stored_cwd", counted)
    report = state.diagnostics(limit=limit)
    expected = Counter()
    for history in HISTORIES:
        rows = report[history]
        assert len(rows) == min(limit, 30)
        expected.update({row["cwd"] for row in rows})
    assert calls == expected


def test_empty_history_does_not_resolve_directories(state, monkeypatch):
    def unexpected(cwd):
        pytest.fail("No directory should be resolved for empty history")

    monkeypatch.setattr(state_module, "_stored_cwd", unexpected)
    report = state.diagnostics()
    assert all(report[history] == [] for history in HISTORIES)
