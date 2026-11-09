"""Read bounded, visible conversation text from Codex JSONL rollouts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from bisect import bisect_left
from collections import deque
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

_MAX_LINE_BYTES = 1_048_576
_TRUNCATED = "\n[... content truncated ...]\n"
_OMITTED = "[... earlier conversation omitted ...]"
_INCOMPLETE = "[Incomplete final transcript record omitted]"
_FINAL_ASSISTANT = "assistant final"
_ROLLOUT_TYPES = {
    "session_meta",
    "response_item",
    "event_msg",
    "turn_context",
    "world_state",
    "compacted",
    "inter_agent_communication_metadata",
    "token_usage_record",
}
_FRAME = re.compile(r"^\[([^\n\]]+); chars=(\d+)\]\n", re.MULTILINE)


class TranscriptError(ValueError):
    """The supplied path is unreadable or is not a supported Codex rollout."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class TranscriptChanged(TranscriptError):
    """A saved cursor no longer identifies the reviewed transcript prefix."""


def _location(path: Path) -> dict[str, Any]:
    location: dict[str, Any] = {
        "path": str(path),
        "file_size": None,
        "mtime_ns": None,
        "line_number": None,
        "line_prefix": None,
    }
    try:
        stat = path.stat()
        location.update(file_size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    except OSError:
        pass
    return location


@contextmanager
def _stream(path: Path, location: dict[str, Any]) -> Generator[BinaryIO]:
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            location.update(file_size=stat.st_size, mtime_ns=stat.st_mtime_ns)
            try:
                yield stream
            except TranscriptError as error:
                remaining = location.get("byte_offset", 0)
                if location["line_number"] is not None:
                    stream.seek(0)
                    line = 1
                    while remaining:
                        chunk = stream.read(min(remaining, _MAX_LINE_BYTES))
                        if not chunk:
                            break
                        line += chunk.count(b"\n")
                        remaining -= len(chunk)
                    location["line_number"] = line
                    error.args = (re.sub(r"(?<=line )\d+$", str(line), str(error)),)
                error.diagnostics = location
                raise
    except OSError as error:
        raise TranscriptError(
            f"Cannot read transcript: {error.strerror or type(error).__name__}",
            diagnostics=location,
        ) from error


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit < len(_TRUNCATED) + 2:
        return text[: _marker_boundary(text, limit - 1, left=True)] + "…"
    remaining = limit - len(_TRUNCATED)
    start = (remaining + 1) // 2
    end = len(text) - (remaining - start)
    start = _marker_boundary(text, start, left=True)
    end = _marker_boundary(text, end, left=False)
    return text[:start] + _TRUNCATED + text[end:]


def _marker_boundary(text: str, position: int, *, left: bool) -> int:
    """Never leave a piece of an earlier truncation marker inside evidence."""
    start = text.rfind(_TRUNCATED, 0, position + len(_TRUNCATED))
    if start < position < start + len(_TRUNCATED) and start >= 0:
        return start if left else start + len(_TRUNCATED)
    return position


def source_blocks(text: str) -> Iterator[tuple[str, str]]:
    """Read length-framed sources without interpreting labels inside their text."""
    offset = 0
    while match := _FRAME.search(text, offset):
        end = match.end() + int(match[2])
        if end > len(text):
            return
        yield match[1], text[match.end() : end]
        offset = end


def frame(label: str, text: str) -> str:
    label = label.replace("\n", " ").replace("\r", " ").replace("]", "")
    return f"[{label}; chars={len(text)}]\n{text}"


def _clip_block(block: str, limit: int) -> str:
    if len(block) <= limit:
        return block
    match = _FRAME.match(block)
    if match is None:
        return _clip(block, limit)
    label, text = match[1], block[match.end() :]
    budget = limit - len(frame(label, "")) - len(str(len(text))) + 1
    if budget < 1:
        return _clip("[Content omitted]", max(1, limit))
    return frame(label, _clip(text, budget))


def _recent_excerpt(
    recent: deque[tuple[int, str]],
    users: deque[tuple[int, str]],
    finals: deque[tuple[int, str]],
    max_chars: int,
) -> str:
    """Reserve recent requests and final replies, then fill with latest activity."""

    def reserve(budget: int) -> dict[int, str]:
        per_user = budget // (3 * max(1, len(users)))
        selected = {index: _clip_block(block, per_user) for index, block in users}
        if max_chars >= 1000:
            per_final = budget // (4 * max(1, len(finals)))
            selected.update((index, _clip_block(block, per_final)) for index, block in finals)
        return selected

    reserved_budget = max_chars
    selected = reserve(reserved_budget)

    def render() -> str:
        parts = []
        previous = 0
        for index, block in sorted(selected.items()):
            if index > previous + 1:
                parts.append(_OMITTED)
            parts.append(block)
            previous = index
        return "\n\n".join(parts)

    # Reclip protected messages together when gap markers consume their reserve.
    # A final reply must stay protected even when it is still in the recent deque.
    while selected and len(render()) > max_chars * 3 // 4:
        reserved_budget = max(1, reserved_budget - 2 * (len(render()) - max_chars * 3 // 4))
        selected = reserve(reserved_budget)
    indexes = sorted(selected)
    used = len(render())

    def gap(before: int, after: int) -> int:
        return len(_OMITTED) + 2 if after > before + 1 else 0

    for index, block in reversed(recent):
        if index in selected:
            available = max_chars - used + len(selected[index])
            replacement = _clip_block(block, available)
            used += len(replacement) - len(selected[index])
            selected[index] = replacement
            continue
        position = bisect_left(indexes, index)
        before = indexes[position - 1] if position else 0
        overhead = (2 if selected else 0) + gap(before, index)
        if position < len(indexes):
            after = indexes[position]
            overhead += gap(index, after) - gap(before, after)
        available = max_chars - used - overhead
        if len(block) <= available:
            selected[index] = block
            indexes.insert(position, index)
            used += overhead + len(block)
            continue
        if available > 0:
            selected[index] = _clip_block(block, available)
        break
    return render()


def _records(
    stream: BinaryIO,
    *,
    complete_only: bool = False,
    location: dict[str, Any] | None = None,
    checkpoint: dict[str, str] | None = None,
    digest: Any = None,
) -> Iterator[tuple[dict | None, str | None]]:
    line_number = 0
    while True:
        start = stream.tell()
        raw = stream.readline(_MAX_LINE_BYTES + 1)
        if not raw:
            return
        record_digest = digest.copy() if digest is not None else None
        if record_digest is not None:
            record_digest.update(raw)
        line_number += 1
        if location is not None:
            location.update(line_number=line_number, line_prefix=repr(raw[:200]), byte_offset=start)
        if len(raw) > _MAX_LINE_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(_MAX_LINE_BYTES + 1)
                if record_digest is not None:
                    record_digest.update(raw)
            if complete_only and not raw.endswith(b"\n"):
                stream.seek(start)
                yield None, _INCOMPLETE
                return
            if checkpoint is not None and record_digest is not None:
                digest = record_digest
                checkpoint["sha256"] = digest.hexdigest()
            yield None, "[Oversized transcript record omitted]"
            continue
        if complete_only and not raw.endswith(b"\n"):
            stream.seek(start)
            yield None, _INCOMPLETE
            return
        if checkpoint is not None and record_digest is not None:
            digest = record_digest
            checkpoint["sha256"] = digest.hexdigest()
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if not raw.endswith(b"\n"):
                yield None, _INCOMPLETE
                return
            raise TranscriptError(f"Invalid JSONL at transcript line {line_number}") from exc
        if not isinstance(row, dict):
            raise TranscriptError(f"Expected a Codex rollout object at line {line_number}")
        yield row, None


def _text_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise TranscriptError("Unsupported transcript content: expected text or content items")
    text = []
    for item in value:
        if not isinstance(item, dict):
            raise TranscriptError("Unsupported transcript content item")
        if item.get("type") in ("input_text", "output_text", "text"):
            if not isinstance(item.get("text"), str):
                raise TranscriptError("Transcript text item is missing its text field")
            text.append(item["text"])
    return "\n".join(text)


def _visible_item(payload: dict) -> tuple[str, bool] | None:
    kind = payload.get("type")
    if kind == "message":
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return None
        if payload.get("channel") == "analysis" or payload.get("phase") == "analysis":
            return None
        if "content" not in payload:
            raise TranscriptError("Transcript message is missing its content field")
        text = _text_content(payload["content"])
        label = role
        if role == "assistant" and (
            payload.get("phase") == "final_answer" or payload.get("channel") == "final"
        ):
            label = _FINAL_ASSISTANT
        return (frame(label, text), role == "user") if text else None
    if kind in {"function_call", "custom_tool_call"}:
        field = "arguments" if kind == "function_call" else "input"
        name = payload.get("name")
        if not isinstance(name, str) or field not in payload:
            raise TranscriptError("Transcript tool call is missing its name or input")
        text = _text_content(payload[field])
        call_id = payload.get("call_id", "unknown")
        return frame(f"tool call: {name}; id={call_id}", text), False
    if kind in {"function_call_output", "custom_tool_call_output"}:
        if "output" not in payload:
            raise TranscriptError("Transcript tool result is missing its output field")
        text = _text_content(payload["output"])
        call_id = payload.get("call_id", "unknown")
        return (frame(f"tool result: id={call_id}", text), False) if text else None
    return None


def _record_payload(row: dict) -> tuple[str, dict | None]:
    kind = row.get("type")
    if not isinstance(kind, str):
        raise TranscriptError("Unsupported transcript format; expected a Codex JSONL rollout")
    if kind not in _ROLLOUT_TYPES:
        return kind, None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        raise TranscriptError("Transcript record is missing its payload object")
    if kind == "response_item" and not isinstance(payload.get("type"), str):
        raise TranscriptError("Transcript response_item is missing its payload type")
    return kind, payload


def _read(
    path: Path,
    max_chars: int,
    *,
    offset: int = 0,
    complete_only: bool = False,
    unknown_types: dict[str, int] | None = None,
    checkpoint: dict[str, str] | None = None,
    expected_checkpoint: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Return visible text, retaining recent user messages and recent work.

    A metadata-only rollout is valid empty history. Missing files, empty files,
    malformed completed records, and unsupported formats raise TranscriptError.
    An incomplete trailing record is tolerated while Codex appends to its rollout.
    """
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 256:
        raise ValueError("max_chars must be an integer of at least 256")
    if expected_checkpoint is not None and checkpoint is None:
        checkpoint = {}
    recent: deque[tuple[int, str]] = deque()
    total = 0
    users: deque[tuple[int, str]] = deque(maxlen=3)
    finals: deque[tuple[int, str]] = deque(maxlen=2)
    omitted = False
    recognized = offset > 0
    sequence = 0
    incomplete_tail = False
    location = _location(path)
    unknown: dict[str, int] = {}
    try:
        with _stream(path, location) as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            if expected_checkpoint is not None:
                if not expected_checkpoint.get("sha256"):
                    raise TranscriptChanged(
                        "Transcript checkpoint is unavailable; review restarted"
                    )
                if expected_checkpoint.get("path") != str(path.resolve()):
                    raise TranscriptChanged("Transcript path changed; review restarted")
            if offset and (checkpoint is not None or expected_checkpoint is not None):
                remaining = offset
                while remaining:
                    chunk = stream.read(min(remaining, _MAX_LINE_BYTES))
                    if not chunk:
                        raise TranscriptChanged("Transcript was truncated; review restarted")
                    digest.update(chunk)
                    remaining -= len(chunk)
            if (
                expected_checkpoint is not None
                and digest.hexdigest() != expected_checkpoint["sha256"]
            ):
                raise TranscriptChanged("Transcript prefix changed; review restarted")
            if checkpoint is not None:
                checkpoint.update(path=str(path.resolve()), sha256=digest.hexdigest())
            stream.seek(offset)
            for row, marker in _records(
                stream,
                complete_only=complete_only,
                location=location,
                checkpoint=checkpoint,
                digest=digest if checkpoint is not None else None,
            ):
                user = False
                final = False
                if row is None:
                    incomplete_tail = marker == _INCOMPLETE
                    block = marker or ""
                else:
                    kind, payload = _record_payload(row)
                    if payload is None:
                        unknown[kind] = unknown.get(kind, 0) + 1
                        continue
                    recognized = True
                    if kind != "response_item":
                        continue
                    visible = _visible_item(payload)
                    if visible is None:
                        continue
                    block, user = visible
                    final = (
                        payload.get("type") == "message"
                        and payload.get("role") == "assistant"
                        and (
                            payload.get("phase") == "final_answer"
                            or payload.get("channel") == "final"
                        )
                    )
                sequence += 1
                if user:
                    users.append((sequence, _clip_block(block, max_chars // 3)))
                if final:
                    finals.append((sequence, _clip_block(block, max_chars // 4)))
                block = _clip_block(block, max_chars - 2)
                recent.append((sequence, block))
                total += len(block) + 2
                while total > max_chars and len(recent) > 1:
                    _, removed = recent.popleft()
                    total -= len(removed) + 2
                    omitted = True
            cursor = stream.tell()
            if checkpoint is not None:
                after = path.stat()
                identity = (before.st_dev, before.st_ino)
                if (after.st_dev, after.st_ino) != identity:
                    raise TranscriptChanged("Transcript file was replaced; review restarted")
                if after.st_size > cursor and not incomplete_tail:
                    raise TranscriptError("Transcript advanced during snapshot validation")
                # Same-size rewrites can share timestamps; validate the bytes unconditionally.
                stream.seek(0)
                digest = hashlib.sha256()
                remaining = cursor
                while remaining:
                    chunk = stream.read(min(remaining, _MAX_LINE_BYTES))
                    if not chunk:
                        raise TranscriptChanged("Transcript was truncated; review restarted")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if digest.hexdigest() != checkpoint["sha256"]:
                    raise TranscriptChanged("Transcript prefix changed; review restarted")
                current = path.stat()
                if (current.st_dev, current.st_ino) != identity:
                    raise TranscriptChanged("Transcript file was replaced; review restarted")
                if (current.st_mtime_ns, current.st_ctime_ns, current.st_size) != (
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                    after.st_size,
                ):
                    raise TranscriptError("Transcript changed during snapshot validation")
        if not recognized:
            raise TranscriptError("Empty or unsupported transcript; expected a Codex JSONL rollout")
    except TranscriptError as exc:
        exc.diagnostics = location
        raise
    except OSError as exc:
        raise TranscriptError(
            f"Cannot read transcript: {exc.strerror or type(exc).__name__}", diagnostics=location
        ) from exc
    if unknown_types is not None:
        unknown_types.update(unknown)
    if not omitted:
        return "\n\n".join(block for _, block in recent), cursor
    return _recent_excerpt(recent, users, finals, max_chars), cursor


def read_transcript(path: Path, max_chars: int = 60_000) -> str:
    return _read(path, max_chars)[0]


def read_snapshot(
    path: Path,
    max_chars: int = 60_000,
    *,
    unknown_types: dict[str, int] | None = None,
    checkpoint: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Return visible complete JSONL records and the byte cursor after them."""
    return _read(
        path, max_chars, complete_only=True, unknown_types=unknown_types, checkpoint=checkpoint
    )


def read_since(path: Path, offset: int, max_chars: int = 60_000) -> str:
    return read_since_snapshot(path, offset, max_chars)[0]


def read_since_snapshot(
    path: Path,
    offset: int,
    max_chars: int = 60_000,
    *,
    expected_checkpoint: dict[str, str] | None = None,
    checkpoint: dict[str, str] | None = None,
) -> tuple[str, int]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    try:
        if path.stat().st_size < offset:
            if expected_checkpoint is not None:
                raise TranscriptChanged("Transcript was truncated; review restarted")
            return "[Transcript was truncated; subsequent context is unavailable]", offset
    except OSError as exc:
        raise TranscriptError(f"Cannot read transcript: {exc.strerror}") from exc
    return _read(
        path,
        max_chars,
        offset=offset,
        complete_only=True,
        checkpoint=checkpoint,
        expected_checkpoint=expected_checkpoint,
    )


def last_assistant_message(path: Path) -> str:
    latest = ""
    recognized = False
    location = _location(path)
    try:
        with _stream(path, location) as stream:
            for row, _ in _records(stream, complete_only=True, location=location):
                if row is None:
                    continue
                kind, payload = _record_payload(row)
                if payload is None:
                    continue
                recognized = True
                if kind != "response_item":
                    continue
                visible = _visible_item(payload)
                if (
                    visible
                    and payload.get("type") == "message"
                    and payload.get("role") == "assistant"
                ):
                    latest = next(source_blocks(visible[0]))[1]
            if not recognized:
                raise TranscriptError(
                    "Empty or unsupported transcript; expected a Codex JSONL rollout"
                )
    except OSError as exc:
        raise TranscriptError(f"Cannot read transcript: {exc.strerror}") from exc
    return latest
