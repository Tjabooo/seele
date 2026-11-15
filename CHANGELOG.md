# Changelog

All notable changes to this project are listed here ([Keep a Changelog](https://keepachangelog.com), [SemVer](https://semver.org)).

## [Unreleased]

- **`seele dm --to NAME_OR_ID [--file PATH ...] [TEXT | -]`**: direct-message a person as the bot (a user id, a
  `<@id>` mention or a name from `[people]`); opens the bot's DM channel through the REST API.
- **Thread lights**: conversation threads are renamed to start with 🟠 (working), 🟢 (finished) or
  🔴 (failed, stopped, cut off by a restart, or waiting for an auto-resume). New prompt threads are
  created with 🟠 already. Renames respect Discord's 2-per-10-minutes limit per thread and only the
  newest light is applied; no permission turns lights off for that thread. After a restart, threads
  still showing 🟠 turn 🔴. New state field `light`.
- **Auto-resume after network outages and API server errors**, not only usage limits. One table
  (`claude.TRANSIENT`) sorts failure texts into `limit`, `network` (`Can't reach the API server`,
  `EAI_AGAIN`, `ENOTFOUND`, `ECONNRESET`, `ETIMEDOUT`, `ECONNREFUSED`, `Connection error`/`lost`,
  `fetch failed`, `socket hang up`, `Request timed out`) and `server` (`server_error`, `overloaded`,
  `api_error`, HTTP 500/502/503/504/529). A failed agent's task-notification, the lead's own error
  result, or its process dying with such text (also in the middle of a later turn) puts the
  conversation in a wait for the API, announced once ("⏸ network down: resuming when the API
  answers"). SEELE then checks `api.anthropic.com` every 30 s (DNS + HTTPS HEAD, no auth); 30 s after
  two good checks in a row the resume goes out with the stopped agents and the cause ("network outage
  04:02–04:32"), as live input if the run is still going, else as a new prompt on the same session.
  A resume that fails the same way within 15 minutes backs off 1, 2, 5, then 10 minutes, with no cap
  while auto-resume is on; `/stop` and autoresume off cancel it. A pending limit resume and an outage
  merge: the later time wins and a limit resume never fires while the API is unreachable (it probes
  first and waits for the network instead). Agents still running when the lead's process dies on an
  outage are listed too. `/status` shows the wait.
- The resume is scheduled before anything is posted, and agent cards, status edits and notices
  survive Discord being unreachable (connection errors, not only HTTP errors): in an outage
  a failed post could swallow the schedule.
- **Hosts: the bot on one machine, Claude on others.** `[hosts]` names machines reached over SSH;
  each conversation runs on one (`/host`, `seele conv --host`, `default_host` for new ones). A host
  with a `mac` is woken with Wake-on-LAN (re-sent every 30 s, up to `wake_timeout`) when it doesn't
  answer. Runs import the host user's systemd session environment, Stop kills the remote process
  group, and folders, `/cd` completion, `/sessions`, `/get`, attachments, `/effort everywhere`, the
  guard check and `/usage` logins all work on the conversation's machine.
- **Windows:** the bot uses Git for Windows' `ssh.exe` (the system OpenSSH 9.5 never passes piped input on); `[ssh] binary` / `options` (e.g. `-F` an alias file) configure it. `install.ps1` (venv + boot-time scheduled task with a restart loop, log file via
  `SEELE_LOG_FILE`); no POSIX-only calls on the bot's path (process groups, uid checks, `pgrep`).
- `[bot] ssh` on a CLI-only machine relays `seele conv` / `seele prompt` to the bot over SSH
  (`seele enqueue` on the bot's machine); `install.sh` then leaves the service off.
- `seele check` lists the hosts and whether they answer.

- **Auto-resume after background agents hit a limit**: an agent whose task-notification failed on a
  usage limit ("You've hit your 5-hour limit · resets 10:10pm (Europe/Stockholm)", weekly wording too)
  now schedules the conversation's auto-resume (reset from the message, else the run's rate-limit event,
  else the usage API; + 5 minutes), announced once. It works while the run goes on, after its turn ended,
  and after it exited. The resume prompt lists the stopped agents with their ids (what `SendMessage`
  takes); the list is persisted and cleared once sent. A resume that comes due while the run is still
  going is written into it as live input instead of waiting for it to end. Several limits at once: the
  later reset wins and a guessed time never replaces a known one. `/status` shows the stopped agents.
- Limit detection also matches "hit your 5-hour/weekly limit" and the `rate_limit` error type.
- `install.sh --no-restart` installs without restarting the service; `bin/seele-restart-later`
  restarts it once no `claude` process runs inside it (a transient user unit).
- `seele history --channel NAME_OR_ID | --server GUILD_ID [--limit N] [--bots] [--json]`: print a
  channel's messages, or every text channel and thread in a server, oldest first (bots left out by default).

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
