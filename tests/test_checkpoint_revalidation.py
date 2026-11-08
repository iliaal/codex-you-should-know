"""Synthetic same-size rewrites on filesystems with colliding timestamps."""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from you_should_know import transcript


def rollout(path: Path, text: str) -> None:
    rows = [
        {"type": "session_meta", "payload": {}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": text},
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_checkpoint_checks_bytes_even_when_rewrite_timestamps_collide(
    tmp_path: Path, monkeypatch, incremental: bool, changed: bool
) -> None:
    path = tmp_path / "synthetic.jsonl"
    rollout(path, "Old failure")
    original_stat = path.stat()
    original_stream = transcript._stream
    original_path_stat = Path.stat
    checkpoint: dict[str, str] = {}
    _, offset = transcript.read_snapshot(path, checkpoint=checkpoint)

    # Only model the timestamp collision; reads, bytes, sizes and inode identity are real.
    def colliding_stat(self, *args, **kwargs):
        result = original_path_stat(self, *args, **kwargs)
        if self != path:
            return result
        fields = {key: getattr(result, key) for key in dir(result) if key.startswith("st_")}
        fields.update(st_mtime_ns=original_stat.st_mtime_ns, st_ctime_ns=original_stat.st_ctime_ns)
        return SimpleNamespace(**fields)

    class RewritingReader:
        def __init__(self, stream):
            self.stream = stream
            self.rewritten = False

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def rewrite(self, data):
            if data and not self.rewritten:
                self.rewritten = True
                rollout(path, "New failure" if changed else "Old failure")
            return data

        def read(self, size):
            return self.rewrite(self.stream.read(size))

        def readline(self, size):
            return self.rewrite(self.stream.readline(size))

    @contextmanager
    def rewriting_stream(*args):
        with original_stream(*args) as stream:
            yield RewritingReader(stream)

    monkeypatch.setattr(Path, "stat", colliding_stat)
    monkeypatch.setattr(transcript, "_stream", rewriting_stream)

    def read():
        if incremental:
            return transcript.read_since_snapshot(path, offset, expected_checkpoint=checkpoint)
        return transcript.read_snapshot(path, checkpoint={})

    if changed:
        with pytest.raises(transcript.TranscriptChanged, match="prefix changed"):
            read()
        assert "New failure" in path.read_text(encoding="utf-8")
    else:
        text, cursor = read()
        assert cursor == offset
        assert text == ("" if incremental else transcript.frame("assistant", "Old failure"))
