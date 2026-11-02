# You Should Know for Codex

Catch consequential oversights while Codex works. You Should Know runs an independent review of recent conversation evidence, confirms a concern is still unresolved before showing it, and remembers notes you mark as known or dismiss.

For example, if you ask Codex to change one setting while preserving the others, and it replaces the entire settings file, the reviewer can flag the lost settings even when Codex claims the task succeeded.

Inspired by Claude Code's `cc-plugin-you-should-know`. Alerts appear automatically in your conversation with numbered options. At the end of the turn, type `ysk 1` to learn more, `ysk 2` to mark the note as known, or `ysk 3` to dismiss it, then press Enter. After you read the details, choose `ysk 3` to dismiss or `ysk 4` to chat in the main session. Bare numbers also work when Codex is not waiting for your answer. These replies work in the Codex CLI without terminal-specific setup; the options are text hints, not clickable buttons.

## Install

You need:

- Linux, WSL, or macOS, with Python 3.11 or newer available as `python3`.
- Git and an authenticated Codex CLI with plugin and hook support. Tested with Codex CLI **0.162.1**.

No Python packages or separate API key are required. Each review uses your existing Codex authentication and consumes model usage. Selected conversation content, including tool results, is sent to the configured Codex model for review.

Run these commands from any directory:

```sh
codex plugin marketplace add iliaal/codex-you-should-know
codex plugin add you-should-know@you-should-know
```

Then activate the hooks:

1. Start a new Codex session.
2. Open `/hooks`.
3. Review and trust all four hooks:
   - `PostToolUse`: **You Should Know: review recent work**
   - `PostToolUse`: **You Should Know: display new notes**
   - `Stop`: **You Should Know: review completed turn**
   - `UserPromptSubmit`: **You Should Know: handle note replies**

Codex skips untrusted hooks. Their commands point to `you-should-know/.../scripts/you_should_know.py`.

The installation applies across projects using the same `CODEX_HOME`, normally `~/.codex`. Other machines or separate Codex profiles need their own installation.

To inspect review status, ask Codex:

```text
You Should Know status
```

An empty check history is normal immediately after installation. After you finish a turn, `status` shows recent check outcomes, candidate counts and reasons, error counts, latency, and reported token usage. Skipped checks, such as cadence, cooldown, and budget skips, are hidden from `recent_checks` unless you pass `--skipped`. Checks that read an unchanged excerpt remain visible. Totals still include skipped checks. Status also shows when each hook last ran and warns when a hook has never run while others have. A missing hook may be untrusted or may not have had an opportunity to run yet. A completed review can return no finding.

Use `status --cwd PATH` to inspect one recorded working directory and `status --limit N` to change the default of 20 rows in each recent-check list. The directory filter applies to sessions, candidate counts and reasons, checks, totals, model usage, transcript errors, hook sightings, warnings, and mutes. Settings and `reviewer_backoff` remain global. Directory matching uses the session's latest recorded working directory, resolves relative paths and symlinks, and excludes records whose session directory is unknown. It does not include subdirectories automatically.

Checks that create or resolve a candidate include its ID, topic, summary, and current status and reason. Checks that publish a note also include a compact note summary. These linked objects reflect their current retained state; the check's own outcome and reason retain the historical result. Older checks have null links, and an expired candidate or note has a null summary. The command does not infer links from matching topics.

Token totals include usage reported before a result failed validation. `usage_unknown_calls` counts calls whose usage was unavailable; those calls are not known to have used zero tokens. Legacy history predating these counters does not acquire invented usage or call counts.

`recent_checks` records the requested model ID and `model_source`: `config` for the plugin setting, `hook` for the active chat's model, or `argument` for a manual `--model` option. `model_usage` groups calls, errors, tokens, and calls with unknown usage by model and source. Review and confirmation calls are recorded separately. Older records and checks without a model call have null model metadata. When the reviewer CLI chooses its own model, the source is `cli_default` and the model ID remains null; do not infer that ID from your current chat or configuration.

Clear initial reviews include a category and short explanation in `diagnostics.no_finding`; the explanation also appears in the check's `reason`. Categories are `no_concern`, `already_disclosed`, `in_progress`, `insufficient_evidence`, `resolved_or_accepted`, and `already_reported`. The `no_finding_reasons` totals honor `--cwd` and count all retained clear initial reviews independently of `--limit` and `--skipped`. A null category represents older history without a classification. These are model-reported reasons for silence, not proof that the work is correct, and require no additional review call.

You can inspect transcript health in `status`:

- Each session reports `unknown_record_types`, counting skipped record types in its latest successful full read, and `consecutive_transcript_failures`, counting failed review checks since its last successful read. Repeated reads do not accumulate unknown-type counts. Cadence and budget skips leave the failure count unchanged.
- `unreviewable_sessions` lists every session with at least `transcript_failure_threshold` consecutive failures; the threshold is 3.
- `recent_transcript_errors` contains the latest 20 checks with read errors, including recovered retries. Each error records the path, file size, modification time in nanoseconds, physical line number, byte offset when available, and an escaped representation of the failing line's first 200 bytes. Unavailable metadata is null. Reply-menu read failures appear with phase `reply`; they do not increment the review failure counter.
- `recent_evidence_rejections` contains the latest 20 checks with rejected evidence. Diagnostics include the reason, escaped quotes, source labels, and an approximate nearest match with a similarity ratio and differing spans. Quote and match displays are capped at 500 characters; at most eight quotes and eight differences per quote are retained, with omitted counts. Approximate matches are diagnostic only and never make a quote valid.

Each affected check also carries its own `diagnostics` object in `recent_checks`. `--limit` applies separately to `recent_checks`, `recent_transcript_errors`, and `recent_evidence_rejections`; it does not limit totals or candidate counts. Diagnostics expire with their check records under `retention_days`. Historical records have no reconstructed diagnostics.

Reviewer subprocess failures include `diagnostics.backend` with a fixed error category, the last observed lifecycle stage, and the effective timeout when a subprocess was started. Recognized retry delays appear as `retry_after_seconds`. Raw subprocess stderr and stdout are not retained. A timeout still does not prove whether networking, inference, or shutdown stalled; the stage narrows where to investigate.

When a parsed reviewer response fails assessment validation, `diagnostics.rejected_response` retains up to 16,384 characters of serialized JSON, plus its original `length` and a `truncated` flag. This lets you inspect a rejected summary or explanation without publishing it as a note. The saved text is unvalidated model output and may contain private conversation excerpts; it expires with the check under `retention_days`. Parse the JSON only when `truncated` is false. Transport failures and older records have no captured response to recover.

When a reviewer reports a rate limit, the plugin stops that request and pauses automatic requests for the same requested model across sessions sharing the data directory. The pause uses the reported retry delay, bounded to 1–3,600 seconds, or 300 seconds when no delay is available. `status` lists active pauses in `reviewer_backoff`, with the model and a Unix `retry_at` timestamp. A null model identifies reviews that use the CLI default. Deferred checks spend no model-call budget, and automatic checks become eligible again after the pause. Explicit manual checks bypass the pause so you can test recovery; a successful manual check does not clear an existing pause.

A failed transcript read gets one retry after 100 ms when the check deadline permits. If it still fails, automatic checks record `transcript_unavailable`. Reads before a reviewer request spend no model-call budget; a request already made before a later read failure remains counted. A pending candidate stays pending if confirmation could not start; a failed read after confirmation starts prevents publication and withdraws it. Manual checks still return an error and a nonzero exit status. This retry mitigates transient unavailability; the cause of the observed historical line-1 failures remains unknown. Codex v0.164.0 [appends rollout records](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/rollout/src/recorder.rs#L2077-L2080), and its [hook transcript path points to the current rollout](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/core/src/session/mod.rs#L5445-L5463).

## Update

See [CHANGELOG.md](CHANGELOG.md) for version history and upgrade notes.

Exit Codex sessions using this plugin before updating. The updater can remove cached hook scripts that running sessions still reference.

If you installed from GitHub, refresh the marketplace and install its current plugin version:

```sh
codex plugin marketplace upgrade you-should-know
codex plugin add you-should-know@you-should-know
```

Start a new session after updating. Review and trust any new or modified definitions in `/hooks`. Your notes and configuration stay in the data directory. Updating from 0.1.1 adds the display and reply hooks; existing notes are preserved without replaying old alerts.

## Use

Continue working in Codex. After six completed tools, the plugin can start a background check. A finding first becomes a hidden pending candidate. At the next eligible check or at turn end, the reviewer checks the candidate against subsequent conversation activity and the assistant's latest messages. Only a concern that remains unresolved and undisclosed becomes a visible note. This adds one confirmation call per candidate, not per check.

When the conversation exceeds the input limits, the plugin prioritizes the latest three user messages and two final assistant replies, then fills remaining space with recent activity in its original order. This keeps current requests and recent disclosures visible during long tool sequences and resumed conversations. Both transcript clipping and the final prompt byte limit preserve these priorities. Long messages can still be shortened, and older requirements can fall outside the excerpt; the reviewer must not infer a problem from omitted context alone.

Initial findings about work still in progress are recorded as suppressed. If confirmation finds the concern fixed, disclosed, still being worked on, or unsupported, the candidate is recorded as withdrawn, with a reason. Confirmation failures caused by a reviewer rate limit, timeout, or recognized connection error retain the candidate for a later confirmation. Other confirmation failures withdraw it. Neither path shows an unconfirmed finding. If the call budget, rate-limit pause, or check time limit prevents confirmation from starting, the candidate stays pending for the next eligible check. At turn end, the plugin reports that pending state instead of silently losing the candidate.

If visible conversation activity arrives while confirmation is running, the candidate is withdrawn because the confirmation no longer covers the latest work. This avoids showing a concern the assistant may already have corrected or disclosed during the call.

The end-of-turn hook waits for an outstanding review, then reviews the finished turn or confirms a pending candidate even during the background review cooldown. This lets the reviewer reassess work it previously suppressed as still in progress. Call budgets still apply, and an unchanged excerpt does not start another review. A final check also covers turns without tools. Confirmed notes appear after a subsequent tool or at turn end, and queued notes survive a restart. Set `final_check = false` to skip blocking reviews and confirmations at turn end; queued notes still appear, while candidates wait for the next eligible background check.

Lock waiting, transcript reading, review, and confirmation share the configured check deadline. Initial reviews can use the full remaining time, including at turn end. Confirmation is deferred when less than 10 seconds remain, or less than half the configured timeout when that is smaller. Deferring preserves the candidate without publishing it; the next eligible check confirms it against the subsequent conversation.

Automatic hooks skip local `codex exec` (including `codex e` and `codex exec resume`) and `codex review` runs when the current process identifies them. These clients do not display the plugin's note text. Skipped hooks make no reviewer requests, spend no check attempts, and leave pending notes untouched. Explicit manual checks remain available in these runs.

Detection uses the current Codex process on Linux, WSL, and macOS. A thread created by `exec` can receive automatic checks when resumed interactively. Unrecognized clients, shared app servers, unreadable process information, and unfamiliar command-line options retain automatic checks. Put image arguments after the subcommand, as in `codex exec --image picture.png`, for reliable detection.

Background checks are at least 120 seconds apart. By default, each session has a rolling budget of 30 model calls per hour, including confirmation calls and failed requests. Unchanged excerpts and other checks that make no model request do not spend that budget. When the budget is exhausted, the plugin shows one notice and retains pending candidates. Calls become available again as earlier calls leave the rolling hour. An explicitly configured `max_checks_per_session` still applies as an additional lifetime limit, including resumed sessions.

The reviewer looks for consequential mistakes, overlooked constraints, and tradeoffs demonstrated by tool results. A user request, bug report, or issue description alone is not proof of a defect. Findings require an exact quote from a tool result; findings contradicting an assistant claim also require a quote from that claim. The reviewer checks whether the assistant already disclosed the concern and can return no note. An alert looks like this:

```text
You Should Know [8f31a7c62b40] · Your previous settings were deleted.

Reply now with a number or phrase (or use ysk N):
[1] Tell me more
[2] Already knew that
[3] Dismiss
```

The initial reply options appear when the turn finishes. Use the `ysk` prefix to distinguish note actions from answers to Codex:

| Reply | Result |
| --- | --- |
| `ysk 1` | Display the explanation and supporting transcript quotes. |
| `ysk 2` | Mark the topic as known so it stays suppressed while retained. |
| `ysk 3` | Dismiss the note and suppress its topic while retained. |

Bare numbers and phrases such as `tell me more` and `dismiss` also work unless the assistant's latest message ends with a question or includes numbered or lettered choices. In those cases, the menu asks for `ysk N`, and unprefixed answers reach the assistant. The prefix is also required when the transcript is unavailable. Reply shortcuts expire after ten minutes; continuing ordinary work pauses them until the next end-of-turn menu.

After you choose **1**, the plugin displays the explanation and evidence with only these options:

```text
[3] Dismiss
[4] Chat in main session
To include feedback, reply "4 your message".
```

The numbers keep the same meaning between menus. Chat becomes available only after you read the details. You can use `dismiss` or `chat in main session` instead of a number.

These replies act on the most recent displayed note in the current session. Every turn-end menu includes the note's summary, even if the notification already appeared during the turn. The full explanation appears only when you ask for details. If you continue ordinary work, the note returns at the next turn end with compact choices. Before you read the details, the reminder looks like this:

```text
You Should Know [8f31a7c62b40] · Your previous settings were deleted.

[1] Tell me more · [2] Already knew that · [3] Dismiss
Reply with a number or phrase.
```

After you read the details, reminders retain only `[3] Dismiss` and `[4] Chat in main session`, including the feedback hint. The menu stage survives resuming the same session.

The reminder repeats at most once per turn until you mark the note as known, dismiss it, or share it using `4`. It appears near the prompt after the turn; it is not pinned during streaming output. Reminders survive resuming the same session and do not require another model review. If several notes are pending, the newest appears first. After you resolve or share it, the next pending note appears at the end of a later turn.

Replies are inactive during work, so they cannot stop a running task. When no note is active, or a choice is unavailable in the displayed menu, replies pass through as ordinary messages without sharing the note. The aliases `ysk more`, `ysk known`, `ysk dismiss`, and `ysk chat` follow the same menu stages.

The first three replies are handled locally. Codex displays a **Hook stopped** heading because the hook answers the reply and stops normal prompt processing. The note stays outside the main agent's context until you choose to share it. Alerts themselves never stop the task.

After reading the details, choose `ysk 4` to hand the selected note to Codex. The agent receives its summary, explanation, and supporting transcript quotes, then responds normally. Sharing does not mark the note as known or dismissed. Add feedback or a request after a space, such as `ysk 4 Please check whether this applies here.` The colon form, `ysk 4: your message`, also works. Your original message reaches the agent unchanged.

This reply does not stop the turn. Once the note is shared, continue the discussion normally; you do not need its ID or a skill command. Sharing a note alone does not ask Codex to change files, but you can include an explicit request to fix the issue.

To revisit older notes or share one with the agent, use an ordinary request such as "Show my You Should Know notes" or "Explain You Should Know note 8f31a7c62b40." The bundled skill and command-line interface remain available for these requests.

To mute reviews and alerts immediately, reply `ysk off` for this session or `ysk off project` for the current working directory across sessions. You do not need an active note. Use `ysk on` or `ysk on project` to remove the corresponding mute. Session and project mutes are independent; removing one does not remove the other or override `enabled = false`. Project matching uses the resolved working-directory path, not every directory in the repository.

### Example: read a note, then discuss it

After an alert appears and Codex finishes its turn:

1. Read the expanded explanation privately:

   ```text
   ysk 1
   ```

   The plugin shows the explanation and supporting transcript quotes. The main agent has not received the note. You now see only `[3] Dismiss` and `[4] Chat in main session`, plus the hint for adding feedback.

2. Pass that context to the main agent, with your own feedback if needed:

   ```text
   ysk 4 This was intentional. Does the concern still apply?
   ```

   Codex receives the same full explanation and evidence you just read, along with your message. It responds normally, and you can continue the discussion without a note ID or skill command.

To share the note without adding a message, reply `ysk 4` alone. If you continue another task first or the reply window expires, wait for the reminder at turn end before replying. Check its summary: the numbered choices always target the latest displayed note.

## Keyboard shortcuts

You can enable optional shortcuts through the bundled setup skill. In Codex, ask:

```text
Set up You Should Know keyboard shortcuts
```

Restart Codex after setup. With Codex CLI **0.164.0**, press **Alt+Y**, release the keys, then press a digit. The digit submits the corresponding reply without Enter:

| Keys | Reply | Action |
| --- | --- | --- |
| Alt+Y, then 1 | `ysk 1` | Read the explanation and evidence. |
| Alt+Y, then 2 | `ysk 2` | Mark the note as already known. |
| Alt+Y, then 3 | `ysk 3` | Dismiss the note. |
| Alt+Y, then 4 | `ysk 4` | Chat in the main session after reading details. |

Use the choices offered by the current note menu. The shortcuts retain the reply window and menu stages; unavailable actions can reach the main agent as ordinary messages. To include feedback, type `ysk 4 your message` instead.

Setup adds the Alt+Y binding and preset slots 1–4 to `${CODEX_HOME:-~/.codex}/config.toml`. It preserves your other settings and comments. If a binding or preset conflicts, setup reports the affected keys and leaves the file unchanged. Settings that already match remain yours. An existing target must be a regular file, not a symlink. Malformed TOML and target tables written inline, such as `tui = { ... }`, may need manual adjustment before setup can add missing settings.

To undo the helper's changes, ask:

```text
Remove You Should Know keyboard shortcuts
```

Removal deletes only unchanged settings marked by the helper. It preserves settings that predated setup and any marked settings you edited afterward. Restart Codex after removal. Setup and removal do not access your notes database or call the reviewer; they do not change the hook definitions.

From a repository clone, you can also run `python3 scripts/you_should_know.py shortcuts install` or `python3 scripts/you_should_know.py shortcuts remove`. Add `--config /path/to/config.toml` to target another Codex configuration file.

## Command-line access

For direct access to notes and settings without asking Codex, clone this repository:

```sh
git clone https://github.com/iliaal/codex-you-should-know.git
cd codex-you-should-know
```

The CLI uses the same data directory as the installed plugin:

```sh
python3 scripts/you_should_know.py notes
python3 scripts/you_should_know.py notes --all
python3 scripts/you_should_know.py notes --cwd /path/to/project
python3 scripts/you_should_know.py show 8f31a7c62b40
python3 scripts/you_should_know.py know 8f31a7c62b40
python3 scripts/you_should_know.py dismiss 8f31a7c62b40
python3 scripts/you_should_know.py status
python3 scripts/you_should_know.py status --cwd /path/to/project --limit 50 --skipped
```

The identifier `8f31a7c62b40` is illustrative; replace it with a published note's identifier from `notes`. The JSON `notes` list contains up to 100 recent open notes, or 100 notes of any status with `--all`. A separate `candidates` list shows pending findings, including their topic, summary, status, reason, timestamps, and recorded directory. `--all` also includes confirmed, suppressed, and withdrawn candidate history. Candidate listings omit the stored transcript, evidence, and full explanation. A pending candidate is unconfirmed; its numeric ID is not a note ID and cannot be used with `show`, `know`, or `dismiss`. Both lists honor `--cwd PATH`. Reading these lists does not call the reviewer or change a finding's status.

To request a check immediately, supply a Codex rollout transcript and session identifier. Manual checks work inside noninteractive sessions and bypass the tool cadence, cooldown, and model-call limits. They still require confirmation before returning a note and can make two model calls:

```sh
python3 scripts/you_should_know.py check --transcript /path/to/rollout.jsonl --session my-check
```

Manual checks record the invoking working directory for the supplied session, so you can find their results with `status --cwd PATH`.

Identical topic labels, ignoring whitespace and case, are suppressed across sessions and projects sharing the data directory. Path punctuation remains significant. The reviewer sees summaries and statuses of up to 40 recent notes, plus a separate list of known and dismissed topic labels within the prompt byte budget. Older known topics can remain in that list after their summaries leave recent history.

By default, state untouched for 30 days is pruned at turn end and when you run `status`, including old notes, candidates, sessions, reminders, interactions, and check history. Pruning also removes obsolete review lock files only when no review holds them. Set `retention_days` to change the period. Pruned topics can appear again, and there is no reopen action for retained notes.

## Configure

Create `config.toml` in `${CODEX_HOME:-~/.codex}/you-should-know`, or set `YSK_DATA_DIR` to choose a different data directory. These are the defaults:

```toml
enabled = true
every_n_tools = 6
min_interval_seconds = 120
max_checks_per_hour = 30
timeout_seconds = 90
max_context_chars = 60000
final_check = true
reply_timeout_seconds = 600
retention_days = 30
debug = false
review_instructions = ""
```

Add `model = "your-model-id"` to select a reviewer model. This setting takes precedence for automatic and manual checks, including over a manual `--model` argument. Without a configured model, automatic checks use the hook's session model when available, then the Codex CLI default. Manual checks accept `--model`; without a configured or explicit model, they use the CLI default.

Set `enabled = false` to disable the hooks globally. Add `max_checks_per_session = 30` only if you also want a lifetime limit; remove that setting from an older configuration to use the rolling limit alone.

To give initial reviews an additional focus, set `review_instructions` to a string of at most 8,000 characters:

```toml
review_instructions = """
Check for recurring costs and configuration that leaves requested features unavailable.
Explain the practical effect in plain language.
"""
```

The default is an empty string. Your instructions supplement the initial review; evidence requirements, output validation, tool restrictions, and separate confirmation still apply. Confirmation uses its existing instructions.

Set `debug = true` or start Codex with `YSK_DEBUG=1` to show a one-line message for each check with its outcome, latency, reported token usage, and requested model and source when a model call was made. Configuration errors include the file path and appear once per session for each changed configuration file, rather than after every tool.

Local note state survives plugin updates and removals, subject to retention. To inspect a separate data directory, place the option before the command:

```sh
python3 scripts/you_should_know.py --data-dir /path/to/ysk-data status
```

## Boundaries

The reviewer and confirmation reviewer run separately with a read-only sandbox. Tool use invalidates either result. Both calls validate evidence quotes against exactly the conversation excerpts shown after budgeting. Each quote must fit within one source block, excluding generated truncation markers and headers. Conversation text is sent as plain text; CRLF, tabs, trailing spaces, backslashes, and Unicode retain their exact spelling without normalization. Previous-note summaries and candidate descriptions cannot supply evidence. This does not promise that every Codex backend can hide every tool from the reviewer.

Checks retain the first three user messages and recent transcript activity within a character budget. A second byte limit bounds the complete model input, dropping older note summaries first and shortening the conversation with a visible marker if necessary. A note can still miss earlier context or be mistaken. Failed or timed-out checks do not interrupt the main task. Use `status` to inspect check state and errors. Native buttons and a separate alert panel would require changes to Codex itself.

Only visible `response_item` content enters the excerpt. Unknown rollout record types are skipped and counted, while arbitrary JSONL without a recognized Codex record type is rejected. `retained_context` and `compacted` checkpoints do not add duplicate answers or encrypted compaction content to the reviewer input.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `codex plugin` or `/hooks` is unavailable | Update Codex to a version with plugin and hook support. This project was tested with CLI 0.162.1. |
| The skill does not appear | Start a new Codex session after installation. Use the full name `$you-should-know:you-should-know`. |
| Hooks are installed but no checks run | Open `/hooks` and confirm all four plugin hooks are enabled and trusted. Check `status` for disabled settings, mutes, budget exhaustion, errors, and missing hook events. |
| No automatic checks run under `codex exec` or `codex review` | These runs skip automatic hooks. Use an explicit manual check if you need a review. |
| A note reply reaches the main agent | Check `status` for a missing `UserPromptSubmit` hook. Trust the reply hook, wait for the end-of-turn menu, and use `ysk N` within the reply window. Bare answers pass through when Codex may be waiting for your answer. |
| A hook is marked **modified** after an update | Review and trust the new definition through `/hooks`. |
| A check reports an authentication failure | Run `codex login`, then try another check. |
| No notes appear after a successful check | Inspect pending candidates with `notes`, or include suppressed and withdrawn history with `notes --all`. Use `status` for check outcomes and aggregate reasons. A clear check found nothing to report. |

## Development

Record user-visible changes under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md). For a release, move those entries into a dated version section and use that section for the GitHub release notes.

From a checkout, install the development tools with [uv](https://docs.astral.sh/uv/) and run the local checks:

```sh
uv --no-config sync --locked
uv --no-config run pytest --cov
uv --no-config run ruff check .
uv --no-config run ruff format --check .
uv --no-config run ty check .
```

The ordinary test suite stubs reviewer calls. Native Codex tests are opt-in: `YSK_TEST_NATIVE_CODEX=1` enables hook discovery, development installation, and rollback tests under isolated `CODEX_HOME` directories. No test makes a reviewer model call. Leave it unset when plugin installation is not authorized.

To run the native installation tests explicitly:

```sh
YSK_TEST_NATIVE_CODEX=1 uv --no-config run pytest tests/test_package.py tests/test_dev_update.py
```

To update and install your development checkout, close Codex chat windows and standalone CLI sessions, then run this in a normal terminal:

```sh
python3 scripts/dev_update.py
```

The updater requires Git, uv, and the Codex CLI. It fetches and merges your branch's tracked upstream, temporarily saving and restoring staged, unstaged, and untracked work. If the merge conflicts, it aborts the merge and restores your work. If restoring your edits conflicts with a successful merge, it stops and retains the edits in a Git stash. It never installs after a merge or validation failure.

Before installation, it installs the locked development dependencies and runs the tests, lint, formatting, type, and whitespace checks. It builds a bundle under `dist/development/` with a version derived from its contents, such as `0.3.3-dev.g0123456789abcdef`, without changing the checkout's published version files.

Installation replaces the existing `you-should-know@you-should-know` plugin and marketplace registration with the local bundle. Other marketplaces, your notes, and your YSK configuration stay unchanged. The updater stops before removal if the installed plugin's enabled state is unavailable or invalid. If installation fails, it attempts to restore the saved installed package and its previous enabled state, even if the original marketplace now contains a different version. It checks the restored plugin identity and marketplace path as well as the version, enabled state, and package contents before reporting successful recovery. Recovery uses a retained local backup and prints its location; the restored marketplace points to that backup instead of GitHub. Keep that directory while it is registered.

Reopen Codex afterward and review and trust the four hooks in `/hooks`. Keep the installed bundle under `dist/development/`, since the local marketplace points to it. Use the updater for subsequent development updates instead of the GitHub update commands above.

To fetch, merge, validate, and build while Codex is open, use `--prepare-only`. Add `--no-fetch` to use your checkout without fetching or merging:

```sh
python3 scripts/dev_update.py --prepare-only --no-fetch
```

The updater uses your current `CODEX_HOME`, normally `~/.codex`. It refuses installation while a live standalone Codex CLI process owned by your user is running. Exited processes awaiting cleanup (zombies) do not block installation. Persistent `codex app-server` and `codex-code-mode-host` services may stay running. The updater cannot detect open app chat windows, so close those yourself before installation.

For a bundle alone, run `python3 scripts/package_plugin.py`. Bundles include runtime files, documentation, and the license, excluding development dependencies, local state, and Git history. Rebuilding replaces only output marked as belonging to the builder. Use a separate `CODEX_HOME` when testing an installation you want to keep separate from your normal profile.

`.codex-plugin/plugin.json` declares the native Codex skill and hooks. `.agents/plugins/marketplace.json` declares the marketplace. Keep the native format: Codex 0.162.1 skips plugin hooks when a portable root `plugin.json` is present.

Hook input and delivery follow the [Codex hooks documentation](https://learn.chatgpt.com/docs/hooks). Installation commands follow the installed CLI's `codex plugin --help` interface.

## License

[MIT](LICENSE). Copyright (c) 2026 Ilia Alshanetsky.
