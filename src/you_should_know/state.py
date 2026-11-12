import contextlib
import errno
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .assessment import Finding, topic_key
from .config import Config

_TRANSCRIPT_FAILURE_THRESHOLD = 3


def _canonical_cwd(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        path = Path(cwd).expanduser().resolve()
        # Python 3.13's non-strict resolve no longer rejects symlink loops.
        try:
            path.stat()
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise
        return str(path)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Cannot resolve working directory: {cwd}") from error


def _stored_cwd(cwd: str | None) -> str:
    try:
        return _canonical_cwd(cwd or "")
    except ValueError:
        return ""


@dataclass(frozen=True)
class Session:
    session_id: str
    events: int
    checked_events: int
    attempts: int
    last_attempt: float
    last_success: float
    last_hash: str
    last_error: str
    touched_at: float = 0
    cwd: str = ""
    consecutive_transcript_failures: int = 0
    unknown_record_types: str = "{}"
    last_transcript_failure: float = 0


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        path = directory / "state.sqlite3"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.create_function("canonical_cwd", 1, _stored_cwd)
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                events INTEGER NOT NULL DEFAULT 0,
                checked_events INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt REAL NOT NULL DEFAULT 0,
                last_success REAL NOT NULL DEFAULT 0,
                last_hash TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                consecutive_transcript_failures INTEGER NOT NULL DEFAULT 0,
                unknown_record_types TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                summary TEXT NOT NULL,
                explanation TEXT NOT NULL,
                evidence TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                note_id TEXT,
                message TEXT
            );
            CREATE INDEX IF NOT EXISTS notifications_session
                ON notifications(session_id, id);
            CREATE TABLE IF NOT EXISTS interactions (
                session_id TEXT PRIMARY KEY,
                note_id TEXT NOT NULL,
                ready INTEGER NOT NULL DEFAULT 0,
                turn_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                topic TEXT NOT NULL,
                summary TEXT NOT NULL,
                explanation TEXT NOT NULL,
                evidence TEXT NOT NULL,
                transcript TEXT NOT NULL,
                offset INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT NOT NULL DEFAULT '',
                assistant_claim INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                touched_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS candidates_session ON candidates(session_id, status);
            CREATE TABLE IF NOT EXISTS attempts (
                session_id TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS attempts_session ON attempts(session_id, created_at);
            CREATE TABLE IF NOT EXISTS notices (
                session_id TEXT NOT NULL,
                key TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(session_id, key)
            );
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event TEXT NOT NULL,
                outcome TEXT NOT NULL,
                phase TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                latency REAL NOT NULL,
                input_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                model_called INTEGER NOT NULL DEFAULT 0,
                usage_reported INTEGER NOT NULL DEFAULT 0,
                model TEXT,
                model_source TEXT,
                diagnostics TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS hook_events (
                session_id TEXT NOT NULL,
                event TEXT NOT NULL,
                seen_at REAL NOT NULL,
                PRIMARY KEY(session_id, event)
            );
            CREATE TABLE IF NOT EXISTS mutes (
                scope TEXT NOT NULL,
                target TEXT NOT NULL,
                touched_at REAL NOT NULL,
                PRIMARY KEY(scope, target)
            );
            CREATE TABLE IF NOT EXISTS transcript_retries (
                session_id TEXT PRIMARY KEY,
                last_failure REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviewer_backoff (
                model TEXT PRIMARY KEY,
                retry_at REAL NOT NULL
            );
        """)
        with self._transaction():
            columns = self.connection.execute("PRAGMA table_info(interactions)").fetchall()
            if not any(column["name"] == "turn_id" for column in columns):
                self.connection.execute(
                    "ALTER TABLE interactions ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''"
                )
            reminders = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'reminders'"
            ).fetchone()
            if reminders is None:
                self.connection.execute(
                    "CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "session_id TEXT NOT NULL, note_id TEXT NOT NULL UNIQUE, "
                    "last_turn_id TEXT NOT NULL DEFAULT '', "
                    "expanded INTEGER NOT NULL DEFAULT 0)"
                )
                self.connection.execute(
                    "CREATE INDEX reminders_session ON reminders(session_id, id)"
                )
                self.connection.execute(
                    "INSERT INTO reminders(session_id, note_id, last_turn_id) "
                    "SELECT interactions.session_id, interactions.note_id, "
                    "CASE WHEN interactions.ready = 1 THEN interactions.turn_id ELSE '' END "
                    "FROM interactions JOIN notes ON notes.id = interactions.note_id "
                    "WHERE notes.status = 'new'"
                )
            else:
                columns = self.connection.execute("PRAGMA table_info(reminders)").fetchall()
                if not any(column["name"] == "expanded" for column in columns):
                    self.connection.execute(
                        "ALTER TABLE reminders ADD COLUMN expanded INTEGER NOT NULL DEFAULT 0"
                    )
            # Schema reads are shared only within this initialization, not across connections.
            table_columns: dict[str, set[str]] = {}
            for table, name, declaration in (
                ("sessions", "touched_at", "REAL NOT NULL DEFAULT 0"),
                ("sessions", "cwd", "TEXT NOT NULL DEFAULT ''"),
                ("sessions", "consecutive_transcript_failures", "INTEGER NOT NULL DEFAULT 0"),
                ("sessions", "unknown_record_types", "TEXT NOT NULL DEFAULT '{}'"),
                ("notes", "touched_at", "REAL NOT NULL DEFAULT 0"),
                ("interactions", "ready_at", "REAL NOT NULL DEFAULT 0"),
                ("notifications", "created_at", "REAL NOT NULL DEFAULT 0"),
                ("candidates", "assistant_claim", "INTEGER NOT NULL DEFAULT 0"),
                ("candidates", "checkpoint", "TEXT NOT NULL DEFAULT '{}'"),
                ("checks", "model_called", "INTEGER NOT NULL DEFAULT 0"),
                ("checks", "usage_reported", "INTEGER NOT NULL DEFAULT 0"),
                ("checks", "model", "TEXT"),
                ("checks", "model_source", "TEXT"),
                ("checks", "diagnostics", "TEXT NOT NULL DEFAULT '{}'"),
                ("checks", "candidate_id", "INTEGER"),
                ("checks", "note_id", "TEXT"),
            ):
                if table not in table_columns:
                    table_columns[table] = {
                        column["name"]
                        for column in self.connection.execute(f"PRAGMA table_info({table})")
                    }
                if name not in table_columns[table]:
                    self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                    table_columns[table].add(name)
                    if table == "interactions":
                        self.connection.execute("UPDATE interactions SET ready = 0")
                    elif table == "notes":
                        self.connection.execute("UPDATE notes SET touched_at = created_at")
                    elif table == "sessions" and name == "touched_at":
                        self.connection.execute(
                            "UPDATE sessions SET touched_at = CASE "
                            "WHEN max(last_attempt, last_success) = 0 THEN ? "
                            "ELSE max(last_attempt, last_success) END",
                            (time.time(),),
                        )
                    elif table == "notifications":
                        self.connection.execute(
                            "UPDATE notifications SET created_at = ?", (time.time(),)
                        )

    def close(self) -> None:
        self.connection.close()

    def observe(self, session_id: str, is_tool: bool, cwd: str = "") -> Session:
        with self.connection:
            self.connection.execute(
                "INSERT INTO sessions(session_id, touched_at, cwd) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET touched_at = excluded.touched_at, "
                "cwd = CASE WHEN excluded.cwd != '' THEN excluded.cwd ELSE sessions.cwd END",
                (session_id, time.time(), _canonical_cwd(cwd)),
            )
            if is_tool:
                self.connection.execute(
                    "UPDATE sessions SET events = events + 1 WHERE session_id = ?", (session_id,)
                )
        return self.session(session_id)

    def session(self, session_id: str) -> Session:
        row = self.connection.execute(
            "SELECT sessions.*, coalesce(transcript_retries.last_failure, 0) "
            "AS last_transcript_failure FROM sessions LEFT JOIN transcript_retries "
            "USING (session_id) WHERE sessions.session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown session")
        return Session(**dict(row))

    def record_transcript_read(
        self,
        session_id: str,
        *,
        unknown_types: dict[str, int] | None = None,
        failed: bool = False,
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE sessions SET consecutive_transcript_failures = CASE WHEN ? "
                "THEN consecutive_transcript_failures + 1 ELSE 0 END, "
                "unknown_record_types = coalesce(?, unknown_record_types), touched_at = ? "
                "WHERE session_id = ?",
                (
                    failed,
                    json.dumps(unknown_types) if unknown_types is not None and not failed else None,
                    time.time(),
                    session_id,
                ),
            )
            if failed:
                self.connection.execute(
                    "INSERT INTO transcript_retries(session_id, last_failure) "
                    "SELECT session_id, ? FROM sessions WHERE session_id = ? "
                    "ON CONFLICT(session_id) DO UPDATE SET last_failure = excluded.last_failure",
                    (time.time(), session_id),
                )
            else:
                self.connection.execute(
                    "DELETE FROM transcript_retries WHERE session_id = ?", (session_id,)
                )

    def begin(self, session_id: str, now: float) -> int:
        with self.connection:
            self.connection.execute(
                "UPDATE sessions SET checked_events = events, attempts = attempts + 1, "
                "last_attempt = ?, touched_at = ? WHERE session_id = ?",
                (now, now, session_id),
            )
            cursor = self.connection.execute(
                "INSERT INTO attempts(session_id, created_at) VALUES (?, ?)", (session_id, now)
            )
            self.connection.execute(
                "DELETE FROM notices WHERE session_id = ? AND key = 'budget'", (session_id,)
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def cancel_attempt(self, session_id: str, attempt_id: int) -> None:
        with self.connection:
            removed = self.connection.execute(
                "DELETE FROM attempts WHERE session_id = ? AND rowid = ?",
                (session_id, attempt_id),
            ).rowcount
            if removed:
                self.connection.execute(
                    "UPDATE sessions SET attempts = attempts - 1 WHERE session_id = ?",
                    (session_id,),
                )

    def finish(self, session_id: str, now: float, digest: str, error: str = "") -> None:
        with self.connection:
            if error:
                self.connection.execute(
                    "UPDATE sessions SET last_error = ? WHERE session_id = ?", (error, session_id)
                )
            else:
                self.connection.execute(
                    "UPDATE sessions SET last_success = ?, last_hash = ?, last_error = '' "
                    "WHERE session_id = ?",
                    (now, digest, session_id),
                )

    def previous(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM notes ORDER BY created_at DESC LIMIT 40"
        ).fetchall()
        return [self._decode(row) for row in rows]

    def known_topics(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT topic FROM notes WHERE status IN ('known', 'dismissed') "
            "ORDER BY touched_at DESC, created_at DESC"
        )
        return list(dict.fromkeys(topic_key(row["topic"]).casefold() for row in rows))

    def has_topic(self, topic: str) -> bool:
        normalized = topic_key(topic).casefold()
        return any(
            topic_key(row["topic"]).casefold() == normalized
            for row in self.connection.execute("SELECT topic FROM notes")
        )

    def budget_available(self, session_id: str, config: Config, now: float) -> bool:
        if (
            config.max_checks_per_session is not None
            and self.session(session_id).attempts >= config.max_checks_per_session
        ):
            return False
        recent = self.connection.execute(
            "SELECT count(*) FROM attempts WHERE session_id = ? AND created_at > ?",
            (session_id, now - 3600),
        ).fetchone()[0]
        return recent < config.max_checks_per_hour

    def reviewer_retry_at(self, model: str | None) -> float:
        row = self.connection.execute(
            "SELECT retry_at FROM reviewer_backoff WHERE model = ?", (model or "",)
        ).fetchone()
        return row["retry_at"] if row else 0.0

    def defer_reviewer(self, model: str | None, now: float, delay: float = 300) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO reviewer_backoff(model, retry_at) VALUES (?, ?) "
                "ON CONFLICT(model) DO UPDATE SET retry_at = "
                "max(reviewer_backoff.retry_at, excluded.retry_at)",
                (model or "", now + max(1, min(3600, delay))),
            )

    def notice_once(self, session_id: str, key: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO notices(session_id, key, created_at) VALUES (?, ?, ?)",
                (session_id, key, time.time()),
            )
        return bool(cursor.rowcount)

    def candidate(
        self,
        session_id: str,
        finding: Finding,
        now: float,
        transcript: str,
        offset: int,
        status: str = "pending",
        reason: str = "",
        assistant_claim: bool = False,
        checkpoint: dict[str, str] | None = None,
    ) -> int:
        if status not in {"pending", "suppressed"}:
            raise ValueError("Invalid initial candidate status")
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO candidates(session_id, topic, summary, explanation, evidence, "
                "transcript, offset, status, reason, created_at, touched_at, assistant_claim, "
                "checkpoint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    finding.topic,
                    finding.summary,
                    finding.explanation,
                    json.dumps(finding.evidence, ensure_ascii=False),
                    transcript,
                    offset,
                    status,
                    reason,
                    now,
                    now,
                    assistant_claim,
                    json.dumps(checkpoint or {}),
                ),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def pending_candidate(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM candidates WHERE session_id = ? AND status = 'pending' "
            "ORDER BY id LIMIT 1",
            (session_id,),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def resolve_candidate(self, identifier: int, status: str, reason: str, now: float) -> bool:
        if status not in {"pending", "confirmed", "withdrawn", "suppressed"}:
            raise ValueError("Invalid candidate status")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE candidates SET status = ?, reason = ?, touched_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, reason, now, identifier),
            )
        return bool(cursor.rowcount)

    def record_check(
        self,
        session_id: str,
        event: str,
        outcome: str,
        now: float,
        latency: float,
        usage: dict[str, int] | None = None,
        reason: str = "",
        phase: str = "review",
        model_called: bool = False,
        model: str | None = None,
        model_source: str | None = None,
        diagnostics: dict[str, Any] | None = None,
        candidate_id: int | None = None,
        note_id: str | None = None,
    ) -> None:
        model_called = model_called or bool(usage)
        usage = usage or {}
        usage_reported = "input_tokens" in usage and "output_tokens" in usage
        with self.connection:
            self.connection.execute(
                "INSERT INTO checks(session_id, event, outcome, phase, reason, created_at, "
                "latency, input_tokens, cached_input_tokens, output_tokens, "
                "model_called, usage_reported, model, model_source, diagnostics, "
                "candidate_id, note_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    event,
                    outcome,
                    phase,
                    reason,
                    now,
                    latency,
                    usage.get("input_tokens", 0),
                    usage.get("cached_input_tokens", 0),
                    usage.get("output_tokens", 0),
                    model_called,
                    usage_reported,
                    model if model_called else None,
                    model_source if model_called else None,
                    json.dumps(diagnostics or {}),
                    candidate_id,
                    note_id,
                ),
            )

    def hook_seen(self, session_id: str, event: str, cwd: str = "") -> None:
        now = time.time()
        with self.connection:
            self.connection.execute(
                "INSERT INTO sessions(session_id, touched_at, cwd) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET touched_at = excluded.touched_at, "
                "cwd = CASE WHEN excluded.cwd != '' THEN excluded.cwd ELSE sessions.cwd END",
                (session_id, now, _canonical_cwd(cwd)),
            )
            self.connection.execute(
                "INSERT INTO hook_events(session_id, event, seen_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id, event) DO UPDATE SET seen_at = excluded.seen_at",
                (session_id, event, now),
            )

    def muted(self, session_id: str, cwd: str = "") -> bool:
        project = str(Path(cwd).resolve()) if cwd else ""
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE mutes SET touched_at = ? WHERE (scope = 'session' AND target = ?) "
                "OR (scope = 'project' AND target = ? AND ? != '')",
                (time.time(), session_id, project, project),
            )
        return bool(cursor.rowcount)

    def mute(self, session_id: str, cwd: str = "", project: bool = False) -> None:
        if project and not cwd:
            raise ValueError("Project mute requires a working directory")
        target = str(Path(cwd).resolve()) if project else session_id
        with self._transaction():
            self.connection.execute(
                "INSERT INTO mutes(scope, target, touched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(scope, target) DO UPDATE SET touched_at = excluded.touched_at",
                ("project" if project else "session", target, time.time()),
            )
            self.connection.execute("DELETE FROM interactions WHERE session_id = ?", (session_id,))

    def unmute(self, session_id: str, cwd: str = "", project: bool = False) -> None:
        if project and not cwd:
            raise ValueError("Project mute requires a working directory")
        target = str(Path(cwd).resolve()) if project else session_id
        with self.connection:
            self.connection.execute(
                "DELETE FROM mutes WHERE scope = ? AND target = ?",
                ("project" if project else "session", target),
            )

    def has_notifications(self, session_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM notifications WHERE session_id = ? LIMIT 1", (session_id,)
            ).fetchone()
            is not None
        )

    @contextlib.contextmanager
    def _transaction(self) -> Generator[None]:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            yield

    def save(
        self, session_id: str, finding: Finding, now: float, notify: bool = False
    ) -> str | None:
        with self._transaction():
            return self._save(session_id, finding, now, notify)

    def _save(self, session_id: str, finding: Finding, now: float, notify: bool) -> str | None:
        normalized = topic_key(finding.topic).casefold()
        key = hashlib.sha256(normalized.encode()).hexdigest()[:12]
        for row in self.connection.execute("SELECT id, topic FROM notes"):
            if topic_key(row["topic"]).casefold() == normalized:
                self.connection.execute(
                    "UPDATE notes SET touched_at = ? WHERE id = ?", (now, row["id"])
                )
                return None
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO notes "
            "(id, session_id, topic, summary, explanation, evidence, created_at, touched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                session_id,
                finding.topic,
                finding.summary,
                finding.explanation,
                json.dumps(finding.evidence, ensure_ascii=False),
                now,
                now,
            ),
        )
        if cursor.rowcount and notify:
            self.connection.execute(
                "INSERT INTO notifications(session_id, note_id, created_at) VALUES (?, ?, ?)",
                (session_id, key, now),
            )
        return key if cursor.rowcount else None

    def publish_candidate(self, identifier: int, now: float, notify: bool = False) -> str | None:
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM candidates WHERE id = ? AND status = 'pending'", (identifier,)
            ).fetchone()
            if row is None:
                return None
            finding = Finding(
                row["topic"], row["summary"], row["explanation"], json.loads(row["evidence"])
            )
            key = self._save(row["session_id"], finding, now, notify)
            self.connection.execute(
                "UPDATE candidates SET status = ?, reason = ?, touched_at = ? WHERE id = ?",
                (
                    "confirmed" if key else "suppressed",
                    "confirmed unresolved and undisclosed" if key else "duplicate topic",
                    now,
                    identifier,
                ),
            )
            return key

    def queue_error(self, session_id: str, message: str) -> None:
        with self._transaction():
            self.connection.execute(
                "INSERT INTO notifications(session_id, message, created_at) VALUES (?, ?, ?)",
                (session_id, message, time.time()),
            )

    def deliver(
        self, session_id: str, ready: bool = False, turn_id: str | None = None
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        notifications = []
        armed_note = None
        now = time.time()
        with self._transaction():
            rows = self.connection.execute(
                "SELECT * FROM notifications WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
            for row in rows:
                if row["note_id"] is None:
                    notifications.append({"message": row["message"]})
                    continue
                note = self.connection.execute(
                    "SELECT * FROM notes WHERE id = ? AND status = 'new'", (row["note_id"],)
                ).fetchone()
                if note is None:
                    continue
                notifications.append({"note": self._decode(note)})
                self.connection.execute(
                    "UPDATE notes SET touched_at = ? WHERE id = ?", (now, note["id"])
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO reminders(session_id, note_id) VALUES (?, ?)",
                    (session_id, note["id"]),
                )
                self.connection.execute(
                    "INSERT INTO interactions(session_id, note_id, ready) VALUES (?, ?, 0) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "note_id = excluded.note_id, ready = 0, turn_id = ''",
                    (session_id, note["id"]),
                )
            self.connection.execute("DELETE FROM notifications WHERE session_id = ?", (session_id,))
            if ready and turn_id:
                note = self.connection.execute(
                    "SELECT notes.*, reminders.last_turn_id, reminders.expanded FROM reminders "
                    "JOIN notes ON notes.id = reminders.note_id "
                    "WHERE reminders.session_id = ? AND notes.status = 'new' "
                    "ORDER BY reminders.id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                if note is not None:
                    self.connection.execute(
                        "UPDATE notes SET touched_at = ? WHERE id = ?", (now, note["id"])
                    )
                    self.connection.execute(
                        "INSERT INTO interactions(session_id, note_id, ready, turn_id, ready_at) "
                        "VALUES (?, ?, 1, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                        "note_id = excluded.note_id, ready = 1, turn_id = excluded.turn_id, "
                        "ready_at = excluded.ready_at",
                        (session_id, note["id"], turn_id, now),
                    )
                    if note["last_turn_id"] != turn_id:
                        armed_note = self._decode(note)
                        armed_note["expanded"] = bool(armed_note["expanded"])
                        last_turn_id = armed_note.pop("last_turn_id")
                        if last_turn_id:
                            notifications.append({"note": armed_note, "reminder": True})
                        self.connection.execute(
                            "UPDATE reminders SET last_turn_id = ? WHERE note_id = ?",
                            (turn_id, note["id"]),
                        )
        return notifications, armed_note

    def reply(
        self,
        session_id: str,
        action: str | None,
        turn_id: str | None = None,
        timeout_seconds: int = 600,
    ) -> dict[str, Any] | None:
        if action not in {None, "more", "known", "dismissed", "chat"}:
            raise ValueError("Invalid note action")
        with self._transaction():
            # A Stop hook can resume the same turn, whose steering must reach the agent.
            note = self.connection.execute(
                "SELECT notes.*, reminders.expanded FROM interactions "
                "JOIN notes ON notes.id = interactions.note_id "
                "JOIN reminders ON reminders.note_id = notes.id "
                "WHERE interactions.session_id = ? AND interactions.ready = 1 "
                "AND interactions.turn_id != '' AND interactions.turn_id != ? "
                "AND ? != '' "
                "AND interactions.ready_at > ? "
                "AND notes.status = 'new'",
                (session_id, turn_id, turn_id, time.time() - timeout_seconds),
            ).fetchone()
            if (
                action is None
                or note is None
                or (note["expanded"] and action in {"more", "known"})
                or (not note["expanded"] and action == "chat")
            ):
                self.connection.execute(
                    "UPDATE interactions SET ready = 0 WHERE session_id = ?", (session_id,)
                )
                return None
            result = self._decode(note)
            result["expanded"] = bool(result["expanded"])
            self.connection.execute(
                "UPDATE notes SET touched_at = ? WHERE id = ?", (time.time(), note["id"])
            )
            if action == "more":
                self.connection.execute(
                    "UPDATE reminders SET expanded = 1 WHERE note_id = ?", (note["id"],)
                )
                result["expanded"] = True
                self.connection.execute(
                    "UPDATE interactions SET ready_at = ? WHERE session_id = ?",
                    (time.time(), session_id),
                )
            if action in {"known", "dismissed"}:
                self.connection.execute(
                    "UPDATE notes SET status = ? WHERE id = ?", (action, note["id"])
                )
                result["status"] = action
            if action != "more":
                self.connection.execute("DELETE FROM reminders WHERE note_id = ?", (note["id"],))
                self.connection.execute(
                    "DELETE FROM interactions WHERE session_id = ?", (session_id,)
                )
            return result

    @staticmethod
    def _cwd_filter(cwd: str | None) -> tuple[str, tuple[str, ...]]:
        if cwd is None:
            return "1", ()
        return (
            "session_id IN (SELECT session_id FROM sessions "
            "WHERE canonical_cwd(cwd) = ? AND cwd != '')",
            (_canonical_cwd(cwd or "."),),
        )

    def notes(self, all_notes: bool = False, cwd: str | None = None) -> list[dict[str, Any]]:
        scope, params = self._cwd_filter(cwd)
        condition = "" if all_notes else "AND notes.status = 'new'"
        rows = self.connection.execute(
            "SELECT notes.*, sessions.cwd FROM notes LEFT JOIN sessions USING (session_id) "
            f"WHERE {scope} {condition} ORDER BY notes.created_at DESC, notes.id DESC LIMIT 100",
            params,
        ).fetchall()
        return [{**self._decode(row), "cwd": _stored_cwd(row["cwd"])} for row in rows]

    def candidates(
        self, cwd: str | None = None, *, all_candidates: bool = False
    ) -> list[dict[str, Any]]:
        scope, params = self._cwd_filter(cwd)
        condition = "" if all_candidates else "AND candidates.status = 'pending'"
        rows = self.connection.execute(
            "SELECT candidates.id, candidates.session_id, candidates.status, candidates.reason, "
            "candidates.topic, candidates.summary, candidates.created_at, candidates.touched_at, "
            "sessions.cwd FROM candidates LEFT JOIN sessions USING (session_id) "
            f"WHERE {scope} {condition} ORDER BY candidates.created_at DESC, candidates.id DESC",
            params,
        )
        return [{**dict(row), "cwd": _stored_cwd(row["cwd"])} for row in rows]

    def _hydrate_checks(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        # Resolve repeated directories once per list, without retaining stale filesystem state.
        directories = {cwd: _stored_cwd(cwd) for cwd in dict.fromkeys(row["cwd"] for row in rows)}
        checks = [
            {
                **dict(row),
                "cwd": directories[row["cwd"]],
                "model_called": bool(row["model_called"]),
                "usage_reported": bool(row["usage_reported"]),
                "diagnostics": json.loads(row["diagnostics"]),
                "candidate": None,
                "note": None,
            }
            for row in rows
        ]
        for table, field, extra in (
            ("candidates", "candidate", ", reason"),
            ("notes", "note", ""),
        ):
            identifiers = list(dict.fromkeys(item[f"{field}_id"] for item in checks))
            identifiers = [identifier for identifier in identifiers if identifier is not None]
            if not identifiers:
                continue
            placeholders = ",".join("?" for _ in identifiers)
            related = {
                row["id"]: dict(row)
                for row in self.connection.execute(
                    f"SELECT id, session_id, status, topic, summary{extra} FROM {table} "
                    f"WHERE id IN ({placeholders})",
                    identifiers,
                )
            }
            for item in checks:
                record = related.get(item[f"{field}_id"])
                if record is not None and record["session_id"] == item["session_id"]:
                    item[field] = {
                        key: value for key, value in record.items() if key != "session_id"
                    }
        return checks

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["evidence"] = json.loads(result["evidence"])
        if "assistant_claim" in result:
            result["assistant_claim"] = bool(result["assistant_claim"])
        if "checkpoint" in result:
            result["checkpoint"] = json.loads(result["checkpoint"])
        result.pop("touched_at", None)
        return result

    def note(self, identifier: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM notes WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown note: {identifier}")
        return self._decode(row)

    def mark(self, identifier: str, status: str) -> None:
        if status not in {"known", "dismissed"}:
            raise ValueError("Invalid note status")
        with self._transaction():
            self.note(identifier)
            self.connection.execute(
                "UPDATE notes SET status = ?, touched_at = ? WHERE id = ?",
                (status, time.time(), identifier),
            )
            self.connection.execute("DELETE FROM interactions WHERE note_id = ?", (identifier,))
            self.connection.execute("DELETE FROM reminders WHERE note_id = ?", (identifier,))

    def status(self, cwd: str | None = None) -> list[dict[str, Any]]:
        scope, params = self._cwd_filter(cwd)
        return [
            {**dict(row), "unknown_record_types": json.loads(row["unknown_record_types"])}
            for row in self.connection.execute(
                f"SELECT * FROM sessions WHERE {scope} ORDER BY last_attempt DESC LIMIT 20",
                params,
            )
        ]

    def diagnostics(
        self, cwd: str | None = None, *, limit: int = 20, include_skipped: bool = False
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("History limit must be a positive integer")
        scope, params = self._cwd_filter(cwd)

        def recent_checks(condition: str = "1") -> list[dict[str, Any]]:
            rows = self.connection.execute(
                "SELECT checks.*, sessions.cwd FROM checks LEFT JOIN sessions USING (session_id) "
                f"WHERE {scope} AND {condition} ORDER BY checks.id DESC LIMIT ?",
                (*params, min(limit, 2**63 - 1)),
            ).fetchall()
            return self._hydrate_checks(rows)

        counts = {"pending": 0, "confirmed": 0, "suppressed": 0, "withdrawn": 0}
        counts.update(
            {
                row["status"]: row["total"]
                for row in self.connection.execute(
                    "SELECT status, count(*) AS total FROM candidates "
                    f"WHERE {scope} GROUP BY status",
                    params,
                )
            }
        )
        reasons = [
            dict(row)
            for row in self.connection.execute(
                "SELECT status, reason, count(*) AS total FROM candidates "
                f"WHERE {scope} AND reason != '' GROUP BY status, reason ORDER BY total DESC",
                params,
            )
        ]
        totals = dict(
            self.connection.execute(
                "SELECT count(*) AS total_checks, "
                "coalesce(sum(outcome = 'error'), 0) AS errors, "
                "coalesce(sum(latency), 0) AS latency_seconds, "
                "coalesce(avg(CASE WHEN latency > 0 THEN latency END), 0) AS average_latency, "
                "coalesce(sum(input_tokens), 0) AS input_tokens, "
                "coalesce(sum(cached_input_tokens), 0) AS cached_input_tokens, "
                "coalesce(sum(output_tokens), 0) AS output_tokens, "
                "coalesce(sum(model_called), 0) AS model_calls, "
                "coalesce(sum(model_called AND NOT usage_reported), 0) AS usage_unknown_calls "
                f"FROM checks WHERE {scope}",
                params,
            ).fetchone()
        )
        hooks: dict[str, dict[str, float]] = {}
        for row in self.connection.execute(
            f"SELECT * FROM hook_events WHERE {scope} ORDER BY session_id, event", params
        ):
            hooks.setdefault(row["session_id"], {})[row["event"]] = row["seen_at"]
        warnings = []
        for session_id, events in hooks.items():
            missing = {
                "Stop",
                "PostToolUse",
                "PostToolUse:notify",
                "UserPromptSubmit",
            } - events.keys()
            for event in sorted(missing):
                warnings.append(
                    {
                        "session_id": session_id,
                        "message": f"{', '.join(sorted(events))} seen, {event} never seen: "
                        f"the {event} hook may be untrusted or has not run yet",
                    }
                )
        return {
            "candidates": counts,
            "reviewer_backoff": [
                {"model": row["model"] or None, "retry_at": row["retry_at"]}
                for row in self.connection.execute(
                    "SELECT model, retry_at FROM reviewer_backoff WHERE retry_at > ? "
                    "ORDER BY model",
                    (time.time(),),
                )
            ],
            "candidate_reasons": reasons,
            "no_finding_reasons": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT json_extract(diagnostics, '$.no_finding.category') AS category, "
                    "count(*) AS total FROM checks "
                    f"WHERE {scope} AND phase = 'review' AND outcome = 'clear' "
                    "GROUP BY category ORDER BY total DESC, category",
                    params,
                )
            ],
            "recent_checks": recent_checks("1" if include_skipped else "outcome != 'skipped'"),
            "recent_transcript_errors": recent_checks(
                "json_array_length(diagnostics, '$.transcript_errors') > 0"
            ),
            "recent_evidence_rejections": recent_checks(
                "json_type(diagnostics, '$.evidence_rejection') = 'object' "
                "AND json_extract(diagnostics, '$.evidence_rejection') != '{}'"
            ),
            "transcript_failure_threshold": _TRANSCRIPT_FAILURE_THRESHOLD,
            "unreviewable_sessions": [
                {**dict(row), "unknown_record_types": json.loads(row["unknown_record_types"])}
                for row in self.connection.execute(
                    f"SELECT * FROM sessions WHERE {scope} "
                    "AND consecutive_transcript_failures >= ? "
                    "ORDER BY consecutive_transcript_failures DESC, touched_at DESC, session_id",
                    (*params, _TRANSCRIPT_FAILURE_THRESHOLD),
                )
            ],
            **totals,
            "model_usage": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT model, model_source, count(*) AS model_calls, "
                    "sum(outcome = 'error') AS errors, "
                    "sum(input_tokens) AS input_tokens, "
                    "sum(cached_input_tokens) AS cached_input_tokens, "
                    "sum(output_tokens) AS output_tokens, "
                    "sum(NOT usage_reported) AS usage_unknown_calls "
                    f"FROM checks WHERE {scope} AND model_called = 1 GROUP BY model, model_source "
                    "ORDER BY model_calls DESC, model, model_source",
                    params,
                )
            ],
            "hooks": hooks,
            "warnings": warnings,
            "mutes": [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM mutes"
                    if cwd is None
                    else "SELECT * FROM mutes WHERE "
                    "(scope = 'project' AND canonical_cwd(target) = ?) OR "
                    "(scope = 'session' AND target IN (SELECT session_id FROM sessions "
                    "WHERE canonical_cwd(cwd) = ? AND cwd != ''))",
                    () if cwd is None else (*params, *params),
                )
            ],
        }

    def prune(self, retention_days: int, now: float) -> dict[str, int]:
        cutoff = now - retention_days * 86_400
        removed: dict[str, int] = {}
        with _prune_guard(self.directory, exclusive=True) as acquired:
            if not acquired:
                return removed
            with self._transaction():
                stale_sessions = [
                    row["session_id"]
                    for row in self.connection.execute(
                        "SELECT session_id FROM sessions WHERE touched_at < ?", (cutoff,)
                    )
                ]
                for table in ("notes", "candidates", "sessions", "mutes"):
                    cursor = self.connection.execute(
                        f"DELETE FROM {table} WHERE touched_at < ?", (cutoff,)
                    )
                    removed[table] = cursor.rowcount
                for table in ("checks", "attempts", "notices"):
                    cursor = self.connection.execute(
                        f"DELETE FROM {table} WHERE created_at < ?", (cutoff,)
                    )
                    removed[table] = cursor.rowcount
                removed["transcript_retries"] = self.connection.execute(
                    "DELETE FROM transcript_retries WHERE session_id NOT IN "
                    "(SELECT session_id FROM sessions)"
                ).rowcount
                removed["reviewer_backoff"] = self.connection.execute(
                    "DELETE FROM reviewer_backoff WHERE retry_at <= ?", (now,)
                ).rowcount
                removed["hook_events"] = self.connection.execute(
                    "DELETE FROM hook_events WHERE seen_at < ?", (cutoff,)
                ).rowcount
                for table in ("reminders", "interactions", "notifications"):
                    removed[table] = self.connection.execute(
                        f"DELETE FROM {table} WHERE note_id IS NOT NULL "
                        "AND note_id NOT IN (SELECT id FROM notes)"
                    ).rowcount
                    for session_id in stale_sessions:
                        removed[table] += self.connection.execute(
                            f"DELETE FROM {table} WHERE session_id = ?", (session_id,)
                        ).rowcount
                removed["notifications"] += self.connection.execute(
                    "DELETE FROM notifications WHERE created_at < ?", (cutoff,)
                ).rowcount
                for session_id in stale_sessions:
                    self.connection.execute(
                        "DELETE FROM mutes WHERE scope = 'session' AND target = ?", (session_id,)
                    )
            active_keys = {
                hashlib.sha256(row["session_id"].encode()).hexdigest()
                for row in self.connection.execute("SELECT session_id FROM sessions")
            }
            removed["locks"] = 0
            for path in self.directory.glob("review-*.lock"):
                key = path.name.removeprefix("review-").removesuffix(".lock")
                if key in active_keys or path.stat().st_mtime >= cutoff:
                    continue
                descriptor = os.open(path, os.O_RDWR)
                try:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    path.unlink()
                    removed["locks"] += 1
                finally:
                    os.close(descriptor)
        return removed


@contextlib.contextmanager
def _prune_guard(
    directory: Path, exclusive: bool = False, *, deadline: float | None = None
) -> Generator[bool]:
    descriptor = os.open(directory / ".review-prune.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        while True:
            try:
                fcntl.flock(descriptor, operation)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic() if deadline is not None else 0
                if remaining <= 0:
                    yield False
                    return
                time.sleep(min(0.05, remaining))
        yield True
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def review_lock(directory: Path, session_id: str, wait_seconds: float = 0) -> Generator[bool]:
    deadline = time.monotonic() + wait_seconds
    with _prune_guard(directory, deadline=deadline) as acquired:
        if not acquired:
            yield False
            return
        key = hashlib.sha256(session_id.encode()).hexdigest()
        descriptor = os.open(directory / f"review-{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        yield False
                        return
                    time.sleep(min(0.05, remaining))
            yield True
        finally:
            os.close(descriptor)
