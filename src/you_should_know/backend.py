from __future__ import annotations

import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import FrameType
from typing import Any

MAX_OUTPUT_BYTES = 1_048_576
MAX_RESULT_BYTES = 65_536
MAX_PROMPT_BYTES = 262_144

_ENVIRONMENT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "CODEX_HOME",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "no_proxy",
    }
)
_DISABLED_FEATURES = (
    "shell_tool",
    "apps",
    "plugins",
    "remote_plugin",
    "hooks",
    "multi_agent",
    "goals",
    "view_image",
    "image_generation",
    "browser_use",
    "computer_use",
    "skill_search",
    "shell_snapshot",
    "memories",
    "tool_suggest",
    "sleep_tool",
    "code_mode",
    "code_mode_host",
    "unbounded_connection_retries",
)
_CONFIG = (
    'web_search="disabled"',
    "project_doc_max_bytes=0",
    "skills.include_instructions=false",
    "skills.bundled.enabled=false",
    "tools.update_plan.enabled=false",
    "tools.experimental_request_user_input.enabled=false",
    "agents.enabled=false",
    'model_reasoning_effort="low"',
    "analytics.enabled=false",
    'history.persistence="none"',
)


class BackendError(RuntimeError):
    """A bounded, non-sensitive error safe to show in hook diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        usage: dict[str, int] | None = None,
        diagnostics: dict[str, Any] | None = None,
        process_started: bool = True,
    ) -> None:
        super().__init__(message)
        self.usage = dict(usage or {})
        self.diagnostics = dict(diagnostics or {})
        self.process_started = process_started


class ReviewResponse(dict[str, Any]):
    def __init__(self, response: dict[str, Any], usage: dict[str, int]) -> None:
        super().__init__(response)
        self.usage = usage


def _terminate(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def _stop(process: subprocess.Popen[bytes]) -> None:
    # The npm launcher can exit before its child; always signal the process group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _failure(error: object) -> BackendError:
    message = error.get("message", "") if isinstance(error, dict) else ""
    message = message.lower() if isinstance(message, str) else ""
    status = re.search(r"\b(?:http|status(?: code)?)\s*[:=]?\s*(401|429)\b", message)
    categories = (
        (
            "authentication",
            ("unauthorized", "authentication", "invalid_api_key", "token could not"),
            "Codex authentication failed; run `codex login` before another check",
        ),
        (
            "rate_limit",
            (
                "rate limit",
                "rate_limit",
                "usage limit",
                "usage_limit",
                "quota",
                "too many requests",
            ),
            "Codex usage or rate limit reached; wait for the limit to reset",
        ),
        (
            "model_unavailable",
            ("model_not_found", "model is not supported", "model does not exist"),
            "Codex reviewer model is unavailable; choose an available model in settings",
        ),
        (
            "schema",
            ("invalid_json_schema", "invalid schema", "response_format"),
            "Codex rejected the review schema; check plugin and model compatibility",
        ),
        (
            "context_limit",
            ("context_length_exceeded", "context length", "maximum context"),
            "Codex review exceeds the model context limit; reduce max_context_chars",
        ),
        (
            "connection",
            (
                "stream disconnected",
                "reconnect",
                "connection",
                "network",
                "error sending request",
                "timed out",
                "bad gateway",
                "service unavailable",
            ),
            "Codex connection failed; check network and service availability",
        ),
    )
    for category, needles, diagnostic in categories:
        expected_status = {"authentication": "401", "rate_limit": "429"}.get(category)
        if (
            status.group(1) == expected_status
            if status
            else any(needle in message for needle in needles)
        ):
            details: dict[str, Any] = {"category": category}
            if category == "rate_limit":
                retry = re.search(
                    r"retrying sampling request \(\d+/\d+ in (\d+(?:\.\d+)?)s\)"
                    r"|retry[-_ ]after[:= ]+(\d+(?:\.\d+)?)(?=$|[\s;,])",
                    message,
                )
                if retry:
                    seconds = float(retry.group(1) or retry.group(2))
                    if math.isfinite(seconds):
                        details["retry_after_seconds"] = min(3600.0, max(1.0, seconds))
            return BackendError(diagnostic, diagnostics=details)
    return BackendError(
        "Codex review failed without a recognized cause; check Codex CLI diagnostics",
        diagnostics={"category": "unknown"},
    )


def _stderr_failure(line: bytes) -> BackendError | None:
    message = line.decode("utf-8", errors="replace").strip()
    logged = re.match(
        r"^(?:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
        r"(?:Z|[+-]\d{2}:\d{2})\s+)?(?:WARN|ERROR)\s+(codex_[\w:]+):\s+(.*)$",
        message,
    )
    if not logged:
        return None
    target, message = logged.groups()
    explicit_status = re.search(
        r"\b429\s+Too Many Requests\b|\b(?:HTTP|status(?: code)?)\s*[:=]?\s*429\b",
        message,
        re.IGNORECASE,
    )
    structured_limit = target == "codex_core::responses_retry" and re.search(
        r"\bsampling_error=.*\b(?:rate|usage)_limit(?:_[a-z]+)*\b", message, re.IGNORECASE
    )
    if not explicit_status and not structured_limit:
        return None
    return _failure({"message": message})


def _event(
    line: bytes,
    usage: dict[str, int] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[bool, BackendError | None]:
    try:
        event = json.loads(line)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise BackendError("Codex returned an invalid event stream") from exc
    if not isinstance(event, dict):
        raise BackendError("Codex returned an invalid event stream")
    kind = event.get("type")
    if not isinstance(kind, str):
        raise BackendError("Codex returned an invalid event stream")
    stages = {
        "thread.started": "thread_started",
        "turn.started": "turn_started",
        "item.started": "responding",
        "item.updated": "responding",
        "item.completed": "responding",
        "turn.completed": "turn_completed",
    }
    if diagnostics is not None and kind in stages:
        diagnostics["stage"] = stages[kind]
    if kind == "turn.failed":
        raise _failure(event.get("error"))
    if kind == "error":
        failure = _failure(event)
        if failure.diagnostics["category"] == "rate_limit":
            raise failure
        # Codex keeps running after error notifications and may recover before turn.completed.
        return False, failure
    if kind in {"item.started", "item.updated", "item.completed"}:
        item = event.get("item")
        if not isinstance(item, dict):
            raise BackendError("Codex returned an invalid item")
        # Events report some actions after execution; this is rejection, not prevention.
        if item.get("type") not in ("agent_message", "reasoning", "error"):
            raise BackendError("Codex attempted tool use; review discarded")
    elif kind not in {"thread.started", "turn.started", "turn.completed"}:
        raise BackendError("Codex returned an unsupported event")
    if kind == "turn.completed" and usage is not None:
        reported = event.get("usage")
        if isinstance(reported, dict):
            for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                value = reported.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    usage[key] = usage.get(key, 0) + value
    return kind == "turn.completed", None


def _run(command: list[str], prompt: bytes, workspace: Path, timeout: float) -> dict[str, int]:
    environment = {key: value for key, value in os.environ.items() if key in _ENVIRONMENT}
    environment["YSK_REVIEWER_ACTIVE"] = "1"
    pending = bytearray()
    stderr_pending = bytearray()
    total = 0
    completed = False
    last_error = None
    usage: dict[str, int] = {}
    diagnostics: dict[str, Any] = {"stage": "starting", "timeout_seconds": timeout}
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as input_file:
        input_file.write(prompt)
        input_file.seek(0)
        try:
            process = subprocess.Popen(
                command,
                stdin=input_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workspace,
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise BackendError(
                "Could not start Codex CLI",
                diagnostics={
                    **diagnostics,
                    "category": (
                        "executable_unavailable"
                        if isinstance(exc, FileNotFoundError)
                        else "startup"
                    ),
                },
                process_started=False,
            ) from exc
        diagnostics["stage"] = "process_started"
        previous_sigterm = None
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.signal(signal.SIGTERM, _terminate)
        try:
            assert process.stdout is not None
            assert process.stderr is not None
            with process.stdout, process.stderr, selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        failure = _stderr_failure(bytes(stderr_pending))
                        if failure is not None:
                            raise failure
                        detail = f". {last_error}" if last_error else ""
                        raise BackendError(
                            f"Codex review timed out{detail}", diagnostics={"category": "timeout"}
                        )
                    for key, _ in selector.select(min(remaining, 0.2)):
                        chunk = os.read(key.fd, 65_536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            if not key.data:
                                failure = _stderr_failure(bytes(stderr_pending))
                                stderr_pending.clear()
                                if failure is not None:
                                    raise failure
                            continue
                        total += len(chunk)
                        if total > MAX_OUTPUT_BYTES:
                            raise BackendError("Codex review exceeded its output limit")
                        if key.data:
                            pending.extend(chunk)
                            if b"\n" not in chunk:
                                continue
                            # Scan offsets instead of copying the remaining buffer for every line.
                            start = 0
                            while (end := pending.find(b"\n", start)) >= 0:
                                line = bytes(pending[start:end])
                                start = end + 1
                                if line.strip():
                                    finished, diagnostic = _event(line, usage, diagnostics)
                                    completed = finished or completed
                                    last_error = diagnostic or last_error
                            del pending[:start]
                        else:
                            stderr_pending.extend(chunk)
                            if b"\n" not in chunk:
                                continue
                            lines = stderr_pending.split(b"\n")
                            stderr_pending = bytearray(lines.pop())
                            for line in lines:
                                failure = _stderr_failure(bytes(line))
                                if failure is not None:
                                    raise failure
                if pending.strip():
                    finished, diagnostic = _event(bytes(pending), usage, diagnostics)
                    completed = finished or completed
                    last_error = diagnostic or last_error
                diagnostics["stage"] = "process_exit"
                try:
                    return_code = process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise BackendError(
                        "Codex review timed out", diagnostics={"category": "timeout"}
                    ) from exc
                if return_code:
                    if last_error:
                        raise BackendError(
                            f"{last_error} (CLI status {return_code})",
                            diagnostics=last_error.diagnostics,
                        )
                    raise BackendError(f"Codex CLI exited with status {return_code}")
                if not completed:
                    raise last_error or BackendError("Codex did not complete the review")
        except BackendError as error:
            error.usage = dict(usage)
            error.diagnostics = {"category": "process", **diagnostics, **error.diagnostics}
            if last_error:
                error.diagnostics["last_error_category"] = last_error.diagnostics["category"]
            raise
        finally:
            try:
                _stop(process)
            finally:
                if previous_sigterm is not None:
                    signal.signal(signal.SIGTERM, previous_sigterm)
    return usage


def review(
    prompt: str,
    schema_path: Path,
    model: str | None = None,
    timeout: float = 60.0,
) -> ReviewResponse:
    """Run an independent restricted CLI review; the caller validates its schema."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise BackendError("Review timeout must be positive and finite", process_started=False)
    encoded = prompt.encode("utf-8")
    if not encoded or len(encoded) > MAX_PROMPT_BYTES:
        raise BackendError(
            "Review prompt is empty or exceeds its size limit", process_started=False
        )
    executable = shutil.which("codex")
    if not executable:
        raise BackendError(
            "Codex CLI was not found on PATH",
            diagnostics={
                "category": "executable_unavailable",
                "stage": "starting",
                "timeout_seconds": timeout,
            },
            process_started=False,
        )
    try:
        schema = schema_path.resolve(strict=True)
    except OSError as exc:
        raise BackendError("Review schema is unavailable", process_started=False) from exc
    with tempfile.TemporaryDirectory(prefix="codex-ysk-") as temporary:
        workspace = Path(temporary)
        output = workspace / "review.json"
        command = [
            executable,
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
            "--json",
            "--color",
            "never",
            "--cd",
            str(workspace),
        ]
        for feature in _DISABLED_FEATURES:
            command.extend(("--disable", feature))
        command.extend(("--enable", "skip_host_skill_discovery"))
        for setting in _CONFIG:
            command.extend(("-c", setting))
        if model:
            command.extend(("--model", model))
        command.append("-")
        usage = _run(command, encoded, workspace, timeout)
        diagnostics = {"category": "result", "stage": "result", "timeout_seconds": timeout}
        try:
            with output.open("rb") as result_file:
                raw = result_file.read(MAX_RESULT_BYTES + 1)
            if len(raw) > MAX_RESULT_BYTES:
                raise BackendError(
                    "Codex review exceeded its result limit", usage=usage, diagnostics=diagnostics
                )
            result = json.loads(raw)
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise BackendError(
                "Codex returned no valid JSON review", usage=usage, diagnostics=diagnostics
            ) from exc
        if not isinstance(result, dict):
            raise BackendError(
                "Codex review must be a JSON object", usage=usage, diagnostics=diagnostics
            )
        return ReviewResponse(result, usage)
