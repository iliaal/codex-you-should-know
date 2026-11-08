from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from you_should_know.backend import BackendError, review


class BackendTests(unittest.TestCase):
    def test_malformed_event_types_preserve_usage_and_safe_diagnostics(self):
        for kind in ([], {}):
            events = [{"type": kind}] + [
                {"type": stage, "item": {"type": kind}}
                for stage in ("item.started", "item.updated", "item.completed")
            ]
            for event in events:
                for ending in ("\n", ""):
                    with self.subTest(event=event, ending=ending):
                        record = {**event, "private": "private-event-canary"}
                        self.cli(
                            "print(json.dumps({'type': 'turn.completed', 'usage': {"
                            "'input_tokens': 123, 'output_tokens': 45}}), flush=True)\n"
                            f"sys.stdout.write({json.dumps(record) + ending!r})\n"
                            "out.write_text('{\"finding\":null}')"
                        )
                        with self.assertRaises(BackendError) as raised:
                            review("test", self.schema, timeout=5)
                        self.assertEqual(
                            raised.exception.usage, {"input_tokens": 123, "output_tokens": 45}
                        )
                        expected_stage = (
                            "responding" if isinstance(event["type"], str) else "turn_completed"
                        )
                        self.assertEqual(
                            raised.exception.diagnostics,
                            {"category": "process", "stage": expected_stage, "timeout_seconds": 5},
                        )
                        self.assertNotIn(
                            "private-event-canary",
                            str(raised.exception) + json.dumps(raised.exception.diagnostics),
                        )

    def test_reported_usage_survives_result_and_late_stream_errors(self):
        for failure in ("json", "oversize", "object", "tool", "exit"):
            with self.subTest(failure=failure):
                self.cli(
                    f"""
                    failure = {failure!r}
                    out.write_text('bad' if failure == 'json' else
                                   'x' * 65537 if failure == 'oversize' else
                                   '[]' if failure == 'object' else '{{"finding":null}}')
                    print(json.dumps({{'type': 'turn.completed', 'usage': {{
                        'input_tokens': 1234, 'output_tokens': 45
                    }}}}), flush=True)
                    if failure == 'tool':
                        print(json.dumps({{'type': 'item.completed', 'item': {{
                            'type': 'command_execution'
                        }}}}))
                    if failure == 'exit':
                        sys.exit(7)
                    """
                )
                with self.assertRaises(BackendError) as raised:
                    review("test", self.schema, timeout=5)
                self.assertEqual(
                    getattr(raised.exception, "usage", {}),
                    {"input_tokens": 1234, "output_tokens": 45},
                )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.schema = self.root / "schema.json"
        self.schema.write_text('{"type":"object"}', encoding="utf-8")
        self.environment = patch.dict(
            os.environ,
            {
                "PATH": str(self.bin),
                "CODEX_HOME": str(self.root / "auth-home"),
                "YSK_TEST_SECRET": "private-environment-canary",
                "OPENAI_API_KEY": "private-provider-canary",
                "AWS_ACCESS_KEY_ID": "private-cloud-canary",
                "NODE_OPTIONS": "--require=/forbidden/code.js",
                "CODEX_THREAD_ID": "parent-thread-canary",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def cli(self, body: str) -> None:
        executable = self.bin / "codex"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, subprocess, sys, time\n"
            f"capture = pathlib.Path({str(self.root)!r})\n"
            "args = sys.argv[1:]\n"
            "out = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
            + textwrap.dedent(body),
            encoding="utf-8",
        )
        executable.chmod(0o700)

    def test_reviews_stdin_in_private_temporary_directory(self) -> None:
        self.cli(
            """
            capture.joinpath('capture.json').write_text(json.dumps({
                'argv': args, 'prompt': sys.stdin.read(), 'cwd': os.getcwd(),
                'mode': pathlib.Path.cwd().stat().st_mode & 0o777,
                'env': dict(os.environ),
            }))
            out.write_text('{"finding":null}')
            print('{"type":"item.completed","item":{"type":"agent_message"}}')
            print('{"type":"turn.completed"}')
            """
        )
        result = review("private-prompt-canary", self.schema, "chosen-model", 5)
        self.assertEqual(result, {"finding": None})
        captured = json.loads((self.root / "capture.json").read_text())
        self.assertEqual(captured["prompt"], "private-prompt-canary")
        self.assertNotIn("private-prompt-canary", captured["argv"])
        self.assertNotEqual(captured["cwd"], os.getcwd())
        self.assertEqual(captured["mode"], 0o700)
        self.assertFalse(Path(captured["cwd"]).exists())
        self.assertEqual(captured["env"]["CODEX_HOME"], str(self.root / "auth-home"))
        self.assertEqual(captured["env"]["YSK_REVIEWER_ACTIVE"], "1")
        for key in (
            "YSK_TEST_SECRET",
            "OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "NODE_OPTIONS",
            "CODEX_THREAD_ID",
        ):
            self.assertNotIn(key, captured["env"])
        arguments = captured["argv"]
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--json"):
            self.assertIn(flag, arguments)
        self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
        disabled = {
            arguments[index + 1]
            for index, argument in enumerate(arguments)
            if argument == "--disable"
        }
        self.assertTrue({"shell_tool", "hooks", "plugins", "apps"} <= disabled)
        self.assertIn('web_search="disabled"', arguments)
        self.assertEqual(arguments[arguments.index("--model") + 1], "chosen-model")

    def test_rejects_tool_events_without_exposing_event_data(self) -> None:
        for kind in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
            with self.subTest(kind=kind):
                self.cli(
                    f"""
                    out.write_text('{{"finding":null}}')
                    print(json.dumps({{'type':'item.started', 'item': {{
                        'type':{kind!r}, 'command':'private-command-canary'}}}}), flush=True)
                    time.sleep(10)
                    """
                )
                started = time.monotonic()
                with self.assertRaisesRegex(BackendError, "tool use") as raised:
                    review("test", self.schema, timeout=5)
                self.assertLess(time.monotonic() - started, 3)
                self.assertNotIn("private-command-canary", str(raised.exception))

    def test_requires_completion_even_when_result_exists(self) -> None:
        self.cli("out.write_text('{\"finding\":null}')")
        with self.assertRaisesRegex(BackendError, "did not complete"):
            review("test", self.schema, timeout=5)

    def test_collects_completed_turn_usage_without_changing_result_keys(self) -> None:
        self.cli(
            """
            out.write_text('{"finding":null}')
            print(json.dumps({'type':'turn.completed','usage': {
                'input_tokens':120, 'cached_input_tokens':80, 'output_tokens':15}}))
            """
        )
        result = review("test", self.schema, timeout=5)
        self.assertEqual(result, {"finding": None})
        self.assertEqual(
            result.usage, {"input_tokens": 120, "cached_input_tokens": 80, "output_tokens": 15}
        )

    def test_usage_handles_absent_and_malformed_counters(self) -> None:
        for usage, expected in (
            (None, {}),
            ({"input_tokens": True, "cached_input_tokens": -1, "output_tokens": "9"}, {}),
            (
                {"input_tokens": 0, "output_tokens": 9, "private_field": 10},
                {"input_tokens": 0, "output_tokens": 9},
            ),
        ):
            with self.subTest(usage=usage):
                self.cli(
                    "out.write_text('{\"finding\":null}')\n"
                    f"sys.stdout.write(json.dumps({{'type':'turn.completed','usage':{usage!r}}}))"
                )
                self.assertEqual(review("test", self.schema, timeout=5).usage, expected)

    def test_nonfatal_warning_items_allow_completed_review(self) -> None:
        self.cli(
            """
            print('{"type":"item.completed","item":{"type":"error","message":"warning"}}')
            print('{"type":"turn.completed"}')
            out.write_text('{"finding":null}')
            """
        )
        self.assertEqual(review("test", self.schema, timeout=5), {"finding": None})

    def test_rejects_failed_turn_without_exposing_private_diagnostics(self) -> None:
        self.cli(
            """
            print('private-stderr-canary', file=sys.stderr)
            print('{"type":"turn.failed","error":{"message":"private-error-canary"}}')
            """
        )
        with self.assertRaisesRegex(BackendError, "review failed") as raised:
            review("test", self.schema, timeout=5)
        self.assertNotIn("private", str(raised.exception))

    def test_allows_codex_recovery_after_error_event(self) -> None:
        self.cli(
            """
            print('{"type":"error","message":"Reconnecting after stream disconnected"}')
            print('{"type":"turn.completed"}')
            out.write_text('{"finding":null}')
            """
        )
        self.assertEqual(review("test", self.schema, timeout=5), {"finding": None})

    def test_stderr_rate_limit_stops_review_and_preserves_safe_retry_details(self) -> None:
        secret = f"private-{self.root.name}"
        self.cli(
            f"""
            print('{{"type":"turn.started"}}', flush=True)
            sys.stderr.write('WARN codex_core::responses_retry: stream disconnected - ')
            sys.stderr.flush()
            time.sleep(0.05)
            sys.stderr.write('retrying sampling request (1/5 in 359.999736269s)... '
                             'sampling_error=exceeded retry limit, last status: '
                             '429 Too Many Requests; {secret}\\n')
            sys.stderr.flush()
            time.sleep(30)
            """
        )
        started = time.monotonic()
        with self.assertRaisesRegex(BackendError, "usage or rate limit") as raised:
            review("test", self.schema, timeout=5)
        self.assertLess(time.monotonic() - started, 3)
        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["category"], "rate_limit")
        self.assertEqual(diagnostics["stage"], "turn_started")
        self.assertEqual(diagnostics["timeout_seconds"], 5)
        self.assertAlmostEqual(diagnostics["retry_after_seconds"], 359.999736269)
        self.assertNotIn(secret, str(raised.exception) + json.dumps(diagnostics))

    def test_timeout_reports_effective_budget_and_last_lifecycle_stage(self) -> None:
        self.cli(
            """
            print('{"type":"turn.started"}', flush=True)
            time.sleep(30)
            """
        )
        with self.assertRaisesRegex(BackendError, "timed out") as raised:
            review("test", self.schema, timeout=0.5)
        self.assertEqual(
            raised.exception.diagnostics,
            {"category": "timeout", "stage": "turn_started", "timeout_seconds": 0.5},
        )

    def test_rate_limit_events_stop_before_cli_retry_and_preserve_reported_usage(self) -> None:
        for event_type in ("error", "turn.failed"):
            with self.subTest(event_type=event_type):
                failure = {"message": "HTTP 429 Too Many Requests; Retry-After: 200"}
                event = {"type": event_type}
                event.update({"error": failure} if event_type == "turn.failed" else failure)
                self.cli(
                    f"""
                    print(json.dumps({{'type': 'turn.completed', 'usage': {{
                        'input_tokens': 123, 'output_tokens': 45
                    }}}}), flush=True)
                    print({json.dumps(event)!r}, flush=True)
                    time.sleep(30)
                    """
                )
                started = time.monotonic()
                with self.assertRaisesRegex(BackendError, "usage or rate limit") as raised:
                    review("test", self.schema, timeout=5)
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(raised.exception.usage, {"input_tokens": 123, "output_tokens": 45})
                self.assertEqual(raised.exception.diagnostics["category"], "rate_limit")
                self.assertEqual(raised.exception.diagnostics["retry_after_seconds"], 200)

    def test_stderr_rate_limit_retry_delays_are_bounded(self) -> None:
        for retry, expected in (("0", 1), ("401.25", 401.25), ("7200", 3600), ("9" * 400, None)):
            with self.subTest(retry=retry):
                message = (
                    "2026-10-28T13:22:44.401Z WARN codex_core::responses_retry: "
                    f"retrying sampling request (1/5 in {retry}s)... "
                    "last status: 429 Too Many Requests"
                )
                self.cli(f"print({message!r}, file=sys.stderr, flush=True)\ntime.sleep(30)")
                with self.assertRaisesRegex(BackendError, "usage or rate limit") as raised:
                    review("test", self.schema, timeout=5)
                self.assertEqual(raised.exception.diagnostics.get("retry_after_seconds"), expected)

    def test_benign_stderr_and_quoted_rate_limit_text_do_not_prevent_success(self) -> None:
        secret = f"private-{self.root.name}"
        for message in (
            f"Processed 429 objects: {secret}",
            f"WARN codex_core::config: processed 429 objects: {secret}",
            '"WARN codex_core::responses_retry: last status: 429 Too Many Requests"',
            "USER: example error: 429 Too Many Requests",
            "USER: WARN codex_core::responses_retry: last status: 429 Too Many Requests",
            "WARN codex_core::config: unknown feature flag rate_limit",
        ):
            with self.subTest(message=message):
                self.cli(
                    f"""
                    print({message!r}, file=sys.stderr)
                    print('{{"type":"turn.completed"}}')
                    out.write_text('{{"finding":null}}')
                    """
                )
                self.assertEqual(review("test", self.schema, timeout=5), {"finding": None})

    def test_split_stderr_retry_delay_is_parsed_only_after_line_finishes(self) -> None:
        self.cli(
            """
            sys.stderr.write('WARN codex_core::responses_retry: '
                             'last status: 429 Too Many Requests; Retry-After: 3')
            sys.stderr.flush()
            time.sleep(0.05)
            sys.stderr.write('60\\n')
            sys.stderr.flush()
            time.sleep(30)
            """
        )
        with self.assertRaisesRegex(BackendError, "usage or rate limit") as raised:
            review("test", self.schema, timeout=5)
        self.assertEqual(raised.exception.diagnostics["retry_after_seconds"], 360)

    def test_unterminated_stderr_is_classified_at_eof_or_deadline(self) -> None:
        for finish in ("exit", "stall"):
            with self.subTest(finish=finish):
                self.cli(
                    f"""
                    sys.stderr.write('WARN codex_core::responses_retry: '
                                     'last status: 429 Too Many Requests; Retry-After: 3')
                    sys.stderr.flush()
                    time.sleep(0.05)
                    sys.stderr.write('60')
                    sys.stderr.flush()
                    if {finish!r} == 'stall':
                        time.sleep(30)
                    """
                )
                started = time.monotonic()
                with self.assertRaisesRegex(BackendError, "usage or rate limit") as raised:
                    review("test", self.schema, timeout=0.5)
                if finish == "stall":
                    self.assertGreaterEqual(time.monotonic() - started, 0.4)
                self.assertEqual(raised.exception.diagnostics["retry_after_seconds"], 360)

    def test_stderr_accepts_explicit_http_status_and_structured_retry_errors(self) -> None:
        for message in (
            "ERROR codex_core::client: request failed with HTTP 429",
            "WARN codex_core::responses_retry: stream disconnected - "
            "retrying sampling request (1/5 in 401.25s)... sampling_error=usage_limit_reached",
        ):
            with self.subTest(message=message):
                self.cli(f"print({message!r}, file=sys.stderr, flush=True)\ntime.sleep(30)")
                with self.assertRaisesRegex(BackendError, "usage or rate limit") as raised:
                    review("test", self.schema, timeout=5)
                self.assertEqual(raised.exception.diagnostics["category"], "rate_limit")

    def test_json_error_numbers_do_not_act_as_status_codes(self) -> None:
        for message, category in (
            ("request took 401.25 seconds; connection timed out", "connection"),
            ("request number 429 failed", "unknown"),
        ):
            with self.subTest(message=message):
                event = {"type": "turn.failed", "error": {"message": message}}
                self.cli(f"print({json.dumps(event)!r})")
                with self.assertRaises(BackendError) as raised:
                    review("test", self.schema, timeout=5)
                self.assertEqual(raised.exception.diagnostics["category"], category)

    def test_completed_turn_with_lingering_process_is_still_bounded(self) -> None:
        self.cli(
            """
            print('{"type":"turn.completed","usage":{"output_tokens":7}}', flush=True)
            time.sleep(30)
            """
        )
        with self.assertRaisesRegex(BackendError, "timed out") as raised:
            review("test", self.schema, timeout=0.5)
        self.assertEqual(raised.exception.diagnostics["stage"], "turn_completed")
        self.assertEqual(raised.exception.usage, {"output_tokens": 7})

    def test_terminal_errors_classify_cause_without_exposing_message(self) -> None:
        cases = (
            ("401 Unauthorized; private-auth-canary", "authentication failed"),
            ("usage_limit_reached; private-usage-canary", "usage or rate limit"),
            ("model_not_found; private-model-canary", "model is unavailable"),
            ("stream disconnected; private-url-canary", "connection failed"),
            ("invalid_json_schema; private-schema-canary", "review schema"),
            ("context_length_exceeded; private-context-canary", "context limit"),
            ("private-unknown-canary", "without a recognized cause"),
        )
        for message, expected in cases:
            with self.subTest(expected=expected):
                self.cli(
                    f"""
                    print(json.dumps({{'type':'error','message':{message!r}}}))
                    print(json.dumps({{'type':'turn.failed','error':{{'message':{message!r}}}}}))
                    """
                )
                with self.assertRaisesRegex(BackendError, expected) as raised:
                    review("test", self.schema, timeout=5)
                self.assertNotIn("private", str(raised.exception))

    def test_error_without_completed_turn_does_not_become_success(self) -> None:
        self.cli(
            """
            print('{"type":"error","message":"429 Too Many Requests; private-canary"}')
            out.write_text('{"finding":null}')
            """
        )
        with self.assertRaisesRegex(BackendError, "usage or rate limit") as raised:
            review("test", self.schema, timeout=5)
        self.assertNotIn("private", str(raised.exception))

    def test_rejects_nonzero_exit_even_when_result_exists(self) -> None:
        self.cli(
            """
            out.write_text('{"finding":null}')
            print('{"type":"turn.completed"}')
            sys.exit(7)
            """
        )
        with self.assertRaisesRegex(BackendError, "status 7"):
            review("test", self.schema, timeout=5)

    def test_rejects_missing_invalid_or_non_object_json(self) -> None:
        for result in (None, "broken", "[]"):
            with self.subTest(result=result):
                write = "" if result is None else f"out.write_text({result!r})"
                self.cli(write + '\nprint(\'{"type":"turn.completed"}\')')
                with self.assertRaises(BackendError):
                    review("test", self.schema, timeout=5)

    def test_bounds_process_output_and_json_result(self) -> None:
        for destination in ("stderr", "result"):
            with self.subTest(destination=destination):
                self.cli(
                    "sys.stderr.write('x' * 1100000)"
                    if destination == "stderr"
                    else "out.write_text('x' * 70000)\nprint('{\"type\":\"turn.completed\"}')"
                )
                with self.assertRaisesRegex(BackendError, "limit"):
                    review("test", self.schema, timeout=5)

    def test_timeout_kills_descendant_when_launcher_exits(self) -> None:
        self.cli(
            """
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            capture.joinpath('child-pid').write_text(str(child.pid))
            sys.exit(0)
            """
        )
        with self.assertRaisesRegex(BackendError, "timed out"):
            review("test", self.schema, timeout=0.5)
        pid = int((self.root / "child-pid").read_text())
        process_state = Path(f"/proc/{pid}/stat")
        for _ in range(100):
            try:
                state = process_state.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            self.fail("review left a running descendant after its timeout")

    def test_cancellation_stops_process_and_removes_temporary_directory(self) -> None:
        self.cli(
            """
            capture.joinpath('cwd').write_text(os.getcwd())
            capture.joinpath('pid').write_text(str(os.getpid()))
            print('{"type":"turn.started"}', flush=True)
            time.sleep(30)
            """
        )
        with patch("you_should_know.backend._event", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                review("test", self.schema, timeout=5)
        self.assertFalse(Path((self.root / "cwd").read_text()).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "pid").read_text()), 0)

    def test_sigterm_kills_reviewer_and_its_descendant(self) -> None:
        self.cli(
            """
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            capture.joinpath('pid').write_text(str(os.getpid()))
            capture.joinpath('child-pid').write_text(str(child.pid))
            capture.joinpath('cwd').write_text(os.getcwd())
            time.sleep(30)
            """
        )
        source = Path(__file__).resolve().parents[1] / "src"
        code = (
            "import sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(source)!r}); "
            "from you_should_know.backend import review; "
            f"review('test', Path({str(self.schema)!r}), timeout=20)"
        )
        with subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as runner:
            try:
                deadline = time.monotonic() + 5
                while not (self.root / "cwd").exists():
                    self.assertIsNone(runner.poll(), "review wrapper exited before cancellation")
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                runner.terminate()
                runner.communicate(timeout=5)
                self.assertEqual(runner.returncode, 128 + signal.SIGTERM)
                self.assertFalse(Path((self.root / "cwd").read_text()).exists())
                for filename in ("pid", "child-pid"):
                    pid = int((self.root / filename).read_text())
                    for _ in range(100):
                        try:
                            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
                        except (FileNotFoundError, ProcessLookupError):
                            break
                        if state == "Z":
                            break
                        time.sleep(0.01)
                    else:
                        self.fail("SIGTERM left a running reviewer process")
            finally:
                if runner.poll() is None:
                    runner.kill()
                for filename in ("pid", "child-pid"):
                    if (self.root / filename).exists():
                        try:
                            os.kill(int((self.root / filename).read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_restores_sigterm_handler_after_review(self) -> None:
        self.cli('out.write_text(\'{"finding":null}\')\nprint(\'{"type":"turn.completed"}\')')
        previous = signal.getsignal(signal.SIGTERM)
        review("test", self.schema, timeout=5)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_invalid_timeout_and_missing_cli_fail_before_launch(self) -> None:
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(BackendError, "timeout"):
                    review("test", self.schema, timeout=timeout)
        with self.assertRaisesRegex(BackendError, "not found"):
            review("test", self.schema)


if __name__ == "__main__":
    unittest.main()
