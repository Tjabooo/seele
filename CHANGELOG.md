# Changelog

All notable changes to this project are listed here ([Keep a Changelog](https://keepachangelog.com), [SemVer](https://semver.org)).

## [Unreleased]

## [0.2.0] - 2026-11-14

Lead mode: long-running sessions with background agents.

- **Live input** (on by default, `[claude] live_input`): Claude Code runs with `--input-format stream-json`
  and stdin stays open; a message sent while it works goes into the running session right away (📨).
  Stdin closes once a turn has ended, no background task runs and nothing new was written; messages after
  that are queued (📥) as before.
- **One reply per turn**: a run that waits for background agents posts each turn's answer as it arrives,
  and the live status moves below it. Empty follow-up turns aren't posted. "Typing…" only shows while a
  turn runs.
- **Agent cards**: `/agents #channel` (or `seele conv --agents-channel`) posts a live card per background
  agent with a thread for its prompt, steps (batched every ~12 s) and final report; cards are kept by task
  id, so a resumed agent reuses its card. Without a channel, running agents are listed in the status.
- **Auto-resume** (`/autoresume`, `seele conv --autoresume`): after a usage limit (reset time from the
  rate-limit event's 5-hour/weekly windows, the limit message, or the usage API), the conversation is
  prompted again 5 minutes after the reset; persisted across restarts, cancelled by a new human prompt or
  `/stop`.
- **CLI**: `seele post` (messages and up to 10 files via the REST API), `seele thread`, `seele conv`,
  `seele prompt` (after the current run, optionally in a fresh session) and `seele usage` (5-hour and
  weekly utilization with reset times, `--json`). Named channels in the new `[channels]` config table.
  `conv`/`prompt` go through a requests folder the bot polls every 3 s. `install.sh` links
  `~/.local/bin/seele`.
- A Discord error while handling an event no longer abandons the Claude process.
- Every prompt now starts with `[Discord message from NAME]` (names from the new `[people]` config table, else the Discord display name), and the system prompt tells Claude several people may use the bot, so it can tell owners apart.

## [0.1.0] - 2026-11-13

- First release.
