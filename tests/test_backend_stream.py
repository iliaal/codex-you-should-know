"""Exercise stdout framing independently of operating-system pipe chunk sizes."""

import io
import json
from collections import deque
from types import SimpleNamespace

import pytest

from you_should_know import backend


def encoded(event):
    return json.dumps(event, ensure_ascii=False).encode()


@pytest.fixture
def run_stream(monkeypatch, tmp_path):
    def run(stdout, width, *, stderr=b"", max_output=None):
        streams = [io.BytesIO(), io.BytesIO()]
        chunks = deque((0, stdout[pos : pos + width]) for pos in range(0, len(stdout), width))
        chunks.extend(((0, b""), (1, stderr), (1, b"")))
        registered = {}
        stopped = []
        seen = []
        original_event = backend._event

        class Selector:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def register(self, stream, events, data):
                fd = streams.index(stream)
                registered[fd] = SimpleNamespace(fd=fd, fileobj=stream, data=data)

            def unregister(self, stream):
                del registered[streams.index(stream)]

            def get_map(self):
                return registered

            def select(self, timeout):
                return [(registered[chunks[0][0]], 1)]

        def read(fd, size):
            expected_fd, chunk = chunks.popleft()
            assert fd == expected_fd
            assert len(chunk) <= size
            return chunk

        def event(line, usage, diagnostics):
            seen.append(line)
            return original_event(line, usage, diagnostics)

        process = SimpleNamespace(stdout=streams[0], stderr=streams[1], wait=lambda **kwargs: 0)
        with monkeypatch.context() as patch:
            patch.setattr(backend.subprocess, "Popen", lambda *args, **kwargs: process)
            patch.setattr(backend.selectors, "DefaultSelector", Selector)
            patch.setattr(backend.os, "read", read)
            patch.setattr(backend, "_stop", lambda process: stopped.append(True))
            patch.setattr(backend, "_event", event)
            if max_output is not None:
                patch.setattr(backend, "MAX_OUTPUT_BYTES", max_output)
            try:
                usage = backend._run(["stub-codex"], b"prompt", tmp_path, 5)
                return usage, seen
            finally:
                assert stopped == [True]
                assert all(stream.closed for stream in streams)

    return run


WIDTHS = (1, 2, 7, 31, 4096, 65_536)
COMPLETED = encoded({"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}})


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
def test_stdout_keeps_event_order_usage_and_partial_tail(run_stream, width, ending):
    events = [
        encoded({"type": "thread.started"}),
        encoded({"type": "error", "message": "Reconnecting after stream disconnected"}),
        encoded({"type": "item.updated", "item": {"type": "agent_message", "text": "café 😀"}}),
        COMPLETED,
        encoded({"type": "turn.started"}),
        COMPLETED,
    ]
    stream = b"\n \t\r\n" + b"\n\n".join(events) + ending
    usage, seen = run_stream(stream, width)
    assert usage == {"input_tokens": 22, "output_tokens": 6}
    assert seen == events[:-1] + [events[-1] + (b"\r" if ending == b"\r\n" else b"")]


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("ending", [b"\n", b""])
@pytest.mark.parametrize(
    ("failure", "message", "category", "stage"),
    [
        (b"not JSON", "invalid event stream", "process", "turn_completed"),
        (
            encoded({"type": "item.started", "item": {"type": "command_execution"}}),
            "tool use",
            "process",
            "responding",
        ),
        (
            encoded({"type": "error", "message": "HTTP 429 Too Many Requests; Retry-After: 12"}),
            "usage or rate limit",
            "rate_limit",
            "turn_completed",
        ),
    ],
)
def test_stdout_late_failure_preserves_only_prior_usage(
    run_stream, width, ending, failure, message, category, stage
):
    stream = COMPLETED + b"\n" + failure + ending
    if ending:
        # A later completion in the same read must not hide the failure or add usage.
        stream += COMPLETED + b"\n"
    with pytest.raises(backend.BackendError, match=message) as caught:
        run_stream(stream, width)
    assert caught.value.usage == {"input_tokens": 11, "output_tokens": 3}
    assert caught.value.diagnostics["category"] == category
    assert caught.value.diagnostics["stage"] == stage
    if category == "rate_limit":
        assert caught.value.diagnostics["retry_after_seconds"] == 12


@pytest.mark.parametrize("width", [1, 65_536])
def test_stdout_whitespace_and_unterminated_events_keep_completion_requirement(run_stream, width):
    with pytest.raises(backend.BackendError, match="did not complete"):
        run_stream(b" \t\r\n" + encoded({"type": "turn.started"}) + b"\n \t", width)


@pytest.mark.parametrize("width", [1, 65_536])
def test_stdout_and_stderr_share_the_output_limit(run_stream, width):
    stdout = COMPLETED + b"\n"
    usage, _ = run_stream(stdout, width, stderr=b"warning\n", max_output=len(stdout) + 8)
    assert usage == {"input_tokens": 11, "output_tokens": 3}
    with pytest.raises(backend.BackendError, match="output limit") as caught:
        run_stream(stdout, width, stderr=b"warning!\n", max_output=len(stdout) + 8)
    assert caught.value.usage == usage


def test_many_stdout_events_in_one_read_keep_every_event(run_stream):
    event = encoded({"type": "turn.started"})
    stdout = (event + b"\n") * 2000 + COMPLETED
    assert len(stdout) <= 65_536
    usage, seen = run_stream(stdout, 65_536)
    assert seen == [event] * 2000 + [COMPLETED]
    assert usage == {"input_tokens": 11, "output_tokens": 3}


@pytest.mark.parametrize("width", WIDTHS)
def test_stdout_completion_survives_later_noncompletion_event(run_stream, width):
    later = encoded({"type": "item.completed", "item": {"type": "agent_message"}})
    usage, seen = run_stream(COMPLETED + b"\n" + later, width)
    assert usage == {"input_tokens": 11, "output_tokens": 3}
    assert seen == [COMPLETED, later]
