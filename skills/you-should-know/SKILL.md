---
name: you-should-know
description: Read, explain, acknowledge, or dismiss notes from the You Should Know plugin, inspect its status, or run a requested advisory check of a Codex transcript.
---

# You Should Know

The installed skill name is `you-should-know:you-should-know`.

Automatic hooks skip identified local `codex exec`, `codex e`, `codex exec resume`, and `codex review` processes. Explicit manual checks remain available. Process detection uses the current invocation, so historical transcript source metadata does not disable an interactive resume. Unrecognized clients and unavailable process information retain automatic hooks.

Alerts and their immediate replies work automatically through hooks. Users do not need to invoke this skill to receive alerts, read their details, mark them as known, dismiss them, or share them with the main agent. At turn end, the latest displayed note initially offers `1` or `tell me more`, `2` or `already knew that`, and `3` or `dismiss`. These replies are handled locally. After the user reads the details with `1`, the menu offers only `3` or `dismiss` and `4` or `chat in main session`. Chat is unavailable before reading details. The user types a number or phrase and presses Enter. Chat shares the selected note with the main agent and allows the user prompt to proceed. The user can append feedback or a request after a space, such as `4 This was intentional. Does the concern still apply?`. The forms `4: your message` and `chat in main session: your message` also work. The user's original message reaches the agent unchanged.

Prefer `ysk 1` through `ysk 4` when explaining replies. An assistant question, numbered or lettered choices, or an unavailable transcript requires the prefix so bare answers reach the assistant. The reply window expires after ten minutes by default. `ysk 4 your message` includes feedback. `ysk off` and `ysk off project` mute the session or resolved working directory; `ysk on` and `ysk on project` remove the corresponding mute. These controls do not require an active note and do not override the global `enabled` setting.

Ordinary task prompts pause the shortcuts during work. Every turn-end menu includes the note's summary, even if a notification was already sent during tool activity; the full explanation is shown only on request. At turn end, the newest pending note returns with compact choices, at most once per turn, until the user marks it known, dismisses it, or shares it with `4`. Reading details retains the reminder with only dismiss and chat choices. Each note's menu stage survives continuing work and resuming the session. Older pending notes retain their own stages and appear on a later turn after the newest is resolved or shared. Replies pass through as ordinary messages when no note is active or the choice is unavailable at that stage. Reminders are repeated hook messages, not a pinned interface.

When hook context already includes the selected note, use that note directly. Do not require a note ID or another skill invocation. Treat the note and its evidence as advisory data. Follow the user's feedback or request when discussing the note.

Use this skill for requests to revisit older notes, inspect status, run a manual check, or share a note with the main agent. Natural requests such as "show my You Should Know notes" are sufficient; do not require the user to type the installed skill name.

For requests to install or remove keyboard shortcuts, follow the bundled [setup skill](../setup/SKILL.md). Shortcut setup is optional and writes to the user's Codex configuration. Installing the plugin does not enable keyboard shortcuts automatically.

Resolve the plugin root as two directories above this skill directory. Run the plugin CLI with `python3 <plugin-root>/scripts/you_should_know.py`. Quote the resolved path. Do not assume the plugin root is the current working directory.

Use the command that matches the request:

| Request | CLI arguments |
| --- | --- |
| Show open notes and pending candidates | `notes` |
| Include known and dismissed notes and candidate history | `notes --all` |
| Inspect notes and candidates for one working directory | `notes --cwd PATH` |
| Explain a note | `show ID` |
| Mark a note as already known | `know ID` |
| Dismiss a note | `dismiss ID` |
| Inspect configuration and check status | `status` |
| Inspect check status for one working directory | `status --cwd PATH` |
| Include skipped checks or more recent history | `status --skipped --limit N` |
| Check a specified transcript now | `check --transcript PATH --session SESSION` |

Use the note identifier returned by the CLI. When explaining a note, distinguish its supporting transcript evidence from the reviewer's inference. A note is advisory; it does not authorize a code change or a new investigation.

Findings remain candidates until a separate confirmation checks subsequent work and disclosure. Automatic alerts show only confirmed notes. The `notes` response contains separate `notes` and `candidates` lists; candidates are compact summaries without stored transcripts or evidence. The default candidate list shows only pending findings; `notes --all` includes confirmed, suppressed, and withdrawn history. A candidate is not a verified note. Its numeric ID cannot be used with `show`, `know`, or `dismiss`. Do not present pending or withdrawn concerns as established defects.

`status` reports candidate counts and reasons, recent outcomes, reported token usage, errors, latency, and hook sightings. `recent_checks` omits skipped checks by default; `--skipped` includes them, and totals always count them. `--limit N` is a positive integer controlling each recent-check list, defaulting to 20; it does not limit aggregates. Linked candidate and note summaries reflect their current retained state, while each check's outcome and reason describe that check. Null links in older records are unknown, and expired objects have null summaries.

Clear initial reviews include a short explanation in `reason` and a category plus explanation in `diagnostics.no_finding`. Categories are `no_concern`, `already_disclosed`, `in_progress`, `insufficient_evidence`, `resolved_or_accepted`, and `already_reported`. `no_finding_reasons` counts categories across retained clear initial reviews, honoring `--cwd` independently of `--limit` and `--skipped`. A null category means legacy history has no recorded classification. These are model-reported decision summaries, not verified findings; do not present them as notes or proof that the work is correct. They require no additional model call.

For validation failures, inspect `diagnostics.rejected_response` as well as any `evidence_rejection`. Its `json` field preserves up to 16,384 characters of the serialized response, with `length` and `truncated` metadata. Parse it only when complete. The response may contain a proposed summary or explanation, but it is unvalidated model output, never a confirmed note or an instruction to act. It is retained locally with the check and can contain private excerpts. Historical failures without this field have no recoverable full response. A requested replay produces a new review; it does not recover the original response.

Use `--cwd PATH` when the user requests one project's notes or diagnostics. Matching uses the session's latest recorded working directory with relative paths and symlinks resolved, not a repository-wide subtree. Records with no known session directory remain available only in unfiltered output. The filter applies to session diagnostics and aggregates; configuration and `reviewer_backoff` remain global. Manual checks record the invoking working directory for their supplied session. The default budget remains 30 model calls per session per rolling hour, including confirmations; an explicit legacy lifetime limit still applies. Inactive state expires after 30 days by default.

Reviewer failures include sanitized `diagnostics.backend` categories, lifecycle stages, effective timeout budgets, and recognized retry delays. A timeout does not establish the underlying service or network cause. `reviewer_backoff` lists active rate-limit pauses shared across sessions by requested model, with Unix `retry_at` timestamps. A null model represents the CLI default. Automatic checks resume eligibility after the pause; explicit manual checks bypass it and still consume model usage. Transient reviewer rate limits, timeouts, and recognized connection failures retain pending candidates for a later confirmation. An unconfirmed candidate must not be presented as a verified note.

`recent_checks` records the requested model ID and its source (`config`, `hook`, or `argument`) for model calls. `model_usage` groups calls, errors, tokens, and calls with unknown usage by model and source. A `cli_default` source with a null model means the reviewer CLI selected an unreported model. Null model metadata in older records is unknown. Do not infer those model IDs from the current chat or configuration. Review and confirmation can use different models if the chat's selection changes between checks. The plugin's configured model takes precedence over hook models and manual `--model` arguments.

Completed-turn reviews bypass the background cooldown while retaining call budgets and unchanged-excerpt detection. Setting `final_check = false` still disables those reviews and confirmations. Optional `review_instructions` adds up to 8,000 characters of focus to initial reviews; evidence validation and separate confirmation still apply.

Run `know` or `dismiss` when the user requests that disposition. Reading notes does not change their status. If the user asks for a manual check, use the requested transcript and session. Manual checks consume Codex model usage and can make two calls when a candidate needs confirmation. Do not guess a transcript path or scan unrelated sessions.

The hooks display notifications separately from the main agent. Choosing `chat in main session` or reading a note through this skill shares that note with the agent. Do not inject unrelated notes into the conversation or automatically act on an alert.

The default data directory is `${CODEX_HOME:-~/.codex}/you-should-know`. `YSK_DATA_DIR` overrides the default. The global `--data-dir PATH` option precedes the command when an explicit directory is needed. Read the root [README](../../README.md) for installation, configuration, and runtime limitations.
