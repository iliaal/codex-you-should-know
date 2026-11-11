# Changelog

## [Unreleased]

### Fixed

- Report transcript JSON integers exceeding Python's digit-conversion limit through normal transcript-error diagnostics and background recovery instead of aborting the check with a bare conversion error.
- Handle JSON exceeding Python's nesting limit in complete transcript records through normal transcript-error diagnostics and recovery. Excessively nested hook input returns the normal advisory error response instead of a traceback.
- Require explicit `ysk` reply shortcuts when the transcript is empty or structurally invalid, so ordinary answers are not mistaken for note actions. Valid metadata-only histories remain supported.
- Report dangling or looping configuration-file symlinks as configuration errors instead of silently restoring enabled review defaults. Repeated hook warnings are suppressed per session and configuration revision when notice tracking is available. Missing configuration files and readable symlinks remain supported.
- Report data-directory symlink loops consistently, including on Python 3.13 and newer and for `CODEX_HOME` or home-directory defaults. The notification hook no longer silently ignores these invalid paths.
- Preserve the normal hook configuration-error response when notice tracking cannot access local state or the configuration file disappears. Duplicate warnings are suppressed when tracking remains available.
- Handle reviewer JSON exceeding Python's nesting limit through normal review-error diagnostics, retaining reported usage and allowing subsequent checks to recover. This covers both streamed events and the final result file.
- Handle invalid data-directory paths through the normal CLI error response instead of a traceback. Shortcut setup and removal no longer resolve the unused notes directory.
- Reject lone Unicode surrogates in reviewer text through normal assessment-error diagnostics before storing findings or decisions. Rejected initial results no longer mark the conversation successfully reviewed, and invalid confirmations withdraw the candidate without publishing a note.
- Report lone Unicode surrogates in visible transcript text and tool labels through normal transcript-error diagnostics and recovery before hashing or requesting a review. Valid Unicode and literal escape text are preserved.
- Handle malformed reviewer event and item types as normal review failures, preserving reported usage and sanitized diagnostics instead of raising an uncaught type error.
- Revalidate checkpoint bytes even when filesystem timestamps are unchanged, so same-size transcript rewrites cannot publish a finding based on stale conversation data.

## [0.3.5] - 2026-11-02

### Fixed

- Ignore exited Codex processes awaiting cleanup (zombies) when checking whether development updates can install. Live standalone CLI sessions still block installation.
- Preserve invalid-directory handling on Python 3.13 and newer: recorded paths containing symlink loops remain unscoped, and explicit directory filters reject them. Missing directories remain valid filters.

### Changed

- Include the note's summary beside every turn-end reply menu, even when its notification appeared earlier in the turn. Full explanations remain available on request, and repeated hooks for the same turn do not duplicate the menu.

Adapted from Donwulff's [zombie-process fix](https://github.com/Donwulff/codex-you-should-know/commit/31601a1cb1bbd7493ca19a8981069bc2b711c580) and [turn-end summaries](https://github.com/Donwulff/codex-you-should-know/commit/d6bb258fcddf708c5a424640e40c9f98b1ca00db).

Start a new Codex session after updating. Hook definitions are unchanged; existing notes, configuration, and confirmation behavior are preserved.

## [0.3.4] - 2026-10-29

### Fixed

- Preserved the latest three user requests and two final assistant replies through transcript and prompt clipping. Recent disclosures remain protected when reserved user text reduces the activity window and when Unicode input triggers the final byte limit.
- Rejected missing or invalid installed enablement metadata before development updates remove anything. Rollback now verifies the restored marketplace path and plugin identity alongside existing package, version, and enabled-state checks.
- Retain pending candidates when the Codex executable disappears or cannot start, including during CLI updates. Failed launches no longer consume the hourly or session model-call budget; the normal background cooldown still applies before retrying.
- Give end-of-turn reviews the full remaining check deadline instead of halving it. Defer confirmation when too little time remains, and retain candidates after reviewer timeouts, rate limits, or recognized connection failures.
- Detect reviewer rate limits in JSON events and recognized stderr errors, stop the affected request, and persist a bounded retry delay shared by automatic checks using the same model. Explicit manual checks can still probe recovery.

### Performance

- Reuse parsed conversation blocks within each evidence-validation decision instead of parsing them again for each quote and source requirement.

### Added

- Added categorized explanations for clear initial reviews and `no_finding_reasons` totals in `status`, including directory-scoped totals and unknown classifications for older records.
- Retained bounded rejected reviewer responses in local check diagnostics so validation failures can be inspected without publishing unconfirmed notes. Existing retention limits apply.
- Added `scripts/dev_update.py` to merge upstream changes, preserve local edits, validate the checkout, and install a versioned development bundle. Failed installations restore a snapshot of the previously installed package and its enabled state; `--prepare-only` validates and builds without changing the installed plugin.
- Include sanitized reviewer failure categories, subprocess stages, timeout budgets, and recognized retry delays in check diagnostics. Show active model pauses in `status` under `reviewer_backoff`.
- Add `notes --cwd` and `status --cwd` to inspect one session working directory, including scoped check history, totals, model usage, transcript diagnostics, hook sightings, warnings, and mutes.
- Show compact pending candidates alongside published notes. `notes --all` includes confirmed, suppressed, and withdrawn candidate history. Candidate listings omit stored transcripts and evidence and do not trigger reviewer calls.
- Add `status --limit` and `status --skipped`. Recent check history hides skipped checks by default while totals retain them. New check records link to compact candidate and note summaries; legacy associations remain unknown.

Context preservation, decision diagnostics, and selected updater checks were adapted from [Donwulff's fork](https://github.com/Donwulff/codex-you-should-know/tree/ad409d14d3dc06bc54e4ffd554eb793b7c021ab7). Exact evidence validation, the existing alert threshold, and withdrawal of confirmations after newer activity remain in place.

Start a new Codex session after updating. Existing notes and configuration are preserved; review any hooks marked modified in `/hooks`.

## [0.3.3] - 2026-10-26

### Fixed

- Validate saved transcript cursors against the source path and a hash of the complete reviewed prefix. If the transcript changes or an older candidate lacks a checkpoint, withdraw that candidate and review the current conversation again. Recheck continuity before publishing a confirmed note.
- Apply the configured background retry interval after transcript-read failures without spending model-call budget. End-of-turn and explicit manual checks still bypass that interval.
- Keep retry timestamps separate from session rows so hooks from 0.3.2 can continue using the shared database during an upgrade.
- Match reviewer output schemas to the existing text-length and evidence-count limits. Clarify that evidence must preserve literal backslashes already present in source text, while escaping the response JSON correctly. Exact evidence and source validation remain unchanged.

### Added

- Added optional shortcut setup through a bundled skill and `shortcuts install`. Press Alt+Y, release the keys, then 1–4 to submit `ysk 1` through `ysk 4` using Codex CLI presets. Existing note-menu stages and reply windows still apply.
- Added `shortcuts remove` to delete only unchanged settings installed by the helper. Setup preserves existing configuration and comments, reports conflicts without writing, and leaves matching pre-existing settings unowned. Neither command accesses the notes database or calls the reviewer; hook definitions are unchanged.

## [0.3.2] - 2026-10-25

### Fixed

- Skipped and counted unknown rollout record types, including `retained_context`, instead of rejecting an otherwise valid Codex rollout. Files must still contain a recognized rollout type. Only visible `response_item` content enters the reviewer excerpt; compaction checkpoints and retained context are not added as duplicate evidence.
- Added one retry for failed transcript reads after 100 ms when the check deadline permits. Persistent failures record `transcript_unavailable` for automatic checks, without spending a model call before review starts. Manual checks retain their error exit status. This is an availability mitigation; source inspection found append-based compaction in Codex v0.164.0, and the historical line-1 corruption cause remains unverified.
- Validated review and confirmation quotes against exactly the conversation excerpts shown after prompt budgeting. Pending candidates retain that same excerpt. Previously, validation could accept text removed from the model's input.
- Accepted exact carriage-return/newline evidence copied from the supplied text. Tabs, trailing spaces, and Unicode remain verbatim; no whitespace or Unicode normalization is applied, and paraphrases remain invalid.
- Required each evidence quote to fit inside a single source block, excluding generated truncation markers and source headers. Exact tool-result evidence, assistant-claim evidence, and the separate confirmation pass remain required. Prompt instructions explicitly exclude labels, omission markers, previous-note summaries, and candidate JSON from evidence.
- Prevented reviewer requests from starting after transcript reading or prompt preparation exhausts the check deadline.

### Added

- Added transcript-error paths, file metadata, physical line numbers, byte offsets, and escaped prefixes of up to 200 bytes to check records. Status exposes recent errors, including successful retries and reply-menu read failures.
- Added skipped unknown record counts and consecutive review transcript failures per session. Status flags every session with at least three consecutive failures; successful reads reset the failure count.
- Added rejected evidence with escaped quote and nearest-match displays capped at 500 characters, similarity ratios, and differing spans to check records. Status exposes recent rejections; diagnostics expire with existing check retention. Approximate matches never affect validation.

The hook definitions are unchanged. No hook re-trust is required for these changes.

## [0.3.1] - 2026-10-25

### Added

- Added the requested reviewer model and its selection source to `status`, with calls, errors, and token usage grouped by model and source. Review and confirmation calls retain their own model choices. Historical records and unreported CLI defaults retain unknown model IDs.
- Added optional `review_instructions` of up to 8,000 characters to focus initial reviews. Evidence validation and separate confirmation still apply; the default review and confirmation prompts are unchanged.

### Fixed

- Allowed changed, completed turns to be reviewed during the background cooldown, including work previously suppressed as still in progress. Call budgets, unchanged-excerpt detection, and `final_check = false` still apply.

### For contributors

- Restored the documented `YSK_TEST_NATIVE_CODEX=1` opt-in for native discovery testing, so ordinary test runs do not install test plugins.

Exit Codex sessions using the plugin before updating, then start or resume Codex. Review and trust any hooks marked modified in `/hooks`. Existing notes and configuration are preserved.

## [0.3.0] - 2026-10-15

### Changed

- Held findings as hidden candidates until a separate confirmation checks subsequent conversation activity and the assistant's latest messages. Only unresolved, undisclosed concerns become notes. Confirmation runs at the next eligible check or at turn end and adds one model call per candidate.
- Required exact tool-result evidence for findings. A claim contradicted by tool output also needs a quote from the assistant. The reviewer now treats requests, bug reports, and issue descriptions as claims to verify, suppresses concerns the assistant is still working on, and checks for prior disclosure.
- Replaced the default lifetime cap with 30 model calls per session per rolling hour, including confirmations. This bounds sustained usage without permanently disabling reviews in long-lived sessions. Existing `max_checks_per_session` settings remain additional lifetime limits; remove that setting to use the rolling limit alone.
- Added `ysk 1` through `ysk 4` replies. When Codex asks a question or presents numbered or lettered choices, only prefixed note replies are intercepted. Missing transcripts also require the prefix. Kept the existing 1–4 meanings, including **3** for Dismiss, so learned replies do not change actions. Reply shortcuts now expire after ten minutes by default.
- Included statuses with recent note summaries and supplied known and dismissed topic labels separately within the prompt budget. Topic matching now ignores case as well as whitespace.

### Added

- Added candidate counts and suppression or withdrawal reasons to `status`, alongside recent outcomes, errors, latency, token totals, and the last occurrence of each hook event. Missing hook events produce a diagnostic hint about trust or a hook that has not run yet.
- Added `debug = true` and `YSK_DEBUG=1` for a one-line check outcome, latency, and token-usage message.
- Added `ysk off` and `ysk off project` to mute a session or working directory, with `ysk on` and `ysk on project` to remove the corresponding mute. Dismissal now includes the mute hint.
- Added `final_check = false` to skip blocking end-of-turn reviews and confirmations while still delivering queued notes. Pending candidates remain for the next eligible background check. The default remains `true`.
- Added configurable 30-day retention for inactive state and safe removal of obsolete review lock files. Pruned known and dismissed topics can appear again.

### Fixed

- Sent conversation evidence as plain text, preserving quotes, newlines, and backslashes without weakening exact-match validation.
- Counted only real model requests toward limits. Budget exhaustion now produces one notice and preserves unconfirmed candidates. Confirmation failures withdraw candidates with a recorded reason.
- Withdrew stale confirmations when the conversation advanced during the call, and prevented concurrent hooks from reopening finalized candidates.
- Stopped repeating a note's summary at turn end when it already appeared during that turn. The reply menu now says "Reply now".
- Reported configuration errors once per session for each changed configuration file, including its path.
- Skipped process-tree detection in the display hook when nothing is queued.
- Retained reported token usage when a result failed validation and distinguished unavailable usage from reported zero tokens.

### For contributors

- Made the native Codex discovery test opt-in with `YSK_TEST_NATIVE_CODEX=1` because it temporarily installs the plugin. Ordinary tests stub reviewer calls and skip that installation test.

Restart Codex after updating. The four hook definitions are unchanged, so this round introduces no hook re-trust requirement. Existing notes are preserved subject to the new retention setting.

## [0.2.6] - 2026-10-15

### Changed

- Disabled automatic reviews, notifications, and note replies in identified local `codex exec`, `codex e`, `codex exec resume`, and `codex review` runs. These clients do not display note text; skipped hooks use no reviewer tokens or check attempts and preserve pending alerts.
- Added detection of the currently running Codex command on Linux, WSL, and macOS so interactive resumes remain eligible regardless of the thread's original source. Explicit manual checks still work in noninteractive runs. Unknown clients or unavailable process information retain automatic checks.

Restart Codex after updating. The four hook definitions and existing notes are unchanged. The 30-attempt session limit and reminder lifecycle are unchanged in this release.

## [0.2.5] - 2026-10-14

### Changed

- Accept feedback after a space in the chat reply, such as `4 hi` or `4 Please check this`. The colon form still works. Updated hints and examples to show the simpler form.

Chat still requires reading the note's details first. Sharing passes the full note and your unchanged message to the main agent, then clears the reminder. Restart Codex after updating; the four hook definitions are unchanged.

## [0.2.4] - 2026-10-14

### Changed

- Split note replies into two stages. Initial alerts offer Tell me more, Already knew that, and Dismiss. After reading details, you see only `[3] Dismiss` and `[4] Chat in main session`, with the hint for including feedback.
- Made chat available only after reading details. Reminders preserve each note's menu stage across ordinary work and session restarts.

Restart Codex after updating. Existing pending notes start with the initial menu; read their details again to enable chat. The four hook definitions are unchanged.

## [0.2.3] - 2026-10-14

### Added

- Added compact end-of-turn reminders for pending notes, so continuing ordinary work brings the note back near the prompt. Reading details keeps the reminder; marking known, dismissing, or sharing with the main agent clears it.
- Preserved older pending reminders when a newer note appears. The newest is shown first, and the next pending note appears on a later turn after you resolve or share it.

Reminders survive resuming the same session and do not make extra reviewer calls. They repeat at most once per turn and are not pinned during streaming output. Updating preserves an active note's reminder without resurfacing historical notes. Restart Codex to load the change; the four hook definitions are unchanged.

## [0.2.2] - 2026-10-14

### Added

- Added numbered reply hints: type `1` to read details, `2` to mark a note as known, `3` to dismiss it, or `4` to discuss it with the main agent, then press Enter. The original phrases still work.
- Added `4: your message` to share the full note and your feedback after reading its details with `1`.

### Changed

- Clarified the README with a two-step example for reading a note privately, then sharing its full context and optional feedback with the main agent.

Restart Codex after updating to load the numbered replies. The four hook definitions are unchanged, and no terminal integration is required.

## [0.2.1] - 2026-10-14

### Added

- Added `chat in main session` and `ysk chat` replies to share the selected note and its evidence with Codex without a note ID or skill command.
- Added optional feedback after a colon, such as `chat in main session: This was intentional. Does the concern still apply?`, so the main agent receives the note and your request together.

The handoff lets Codex respond normally and leaves the note's status unchanged. Other note replies remain local. Restart Codex after updating to load the new reply action; the four hook definitions are unchanged.

## [0.2.0] - 2026-10-14

### Added

- Added the replies `tell me more`, `already knew that`, and `dismiss` for the latest displayed note after a turn ends, without invoking a skill or sharing the note with the main agent.
- Preserved queued alerts when resuming the same session after a restart.
- Added named hooks for displaying notes and handling replies.

### Changed

- Displayed completed background findings and check failures inline in the conversation instead of Codex's asynchronous warning UI.

### Fixed

- Delivered findings that finish while the end-of-turn hook waits for a background review, even when the cooldown prevents another check.

### Upgrade

Exit Codex sessions using this plugin before updating; the updater can remove cached hook scripts still used by those sessions. After updating, start or resume Codex and trust **You Should Know: display new notes** and **You Should Know: handle note replies** in `/hooks`. Existing notes and configuration are preserved; old notes are not replayed as new alerts.

## [0.1.1] - 2026-10-14

Initial public version.

### Added

- Added independent conversation reviews after tool use and at turn end, using existing Codex authentication.
- Added persistent notes, known and dismissed topic tracking, configurable review cadence, and manual checks through a bundled skill and CLI.
- Named the review hooks in Codex's trust interface.

[Unreleased]: https://github.com/iliaal/codex-you-should-know/compare/v0.3.5...HEAD
[0.3.5]: https://github.com/iliaal/codex-you-should-know/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/iliaal/codex-you-should-know/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/iliaal/codex-you-should-know/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/iliaal/codex-you-should-know/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/iliaal/codex-you-should-know/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/iliaal/codex-you-should-know/compare/v0.2.6...v0.3.0
[0.2.6]: https://github.com/iliaal/codex-you-should-know/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/iliaal/codex-you-should-know/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/iliaal/codex-you-should-know/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/iliaal/codex-you-should-know/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/iliaal/codex-you-should-know/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/iliaal/codex-you-should-know/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/iliaal/codex-you-should-know/releases/tag/v0.2.0
[0.1.1]: https://github.com/iliaal/codex-you-should-know/tree/3dee356fdd41ec0e8542862f094a09afbf1c07ba
