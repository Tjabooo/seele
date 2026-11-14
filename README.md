# seele

[![CI](https://github.com/Tjabooo/seele/actions/workflows/ci.yml/badge.svg)](https://github.com/Tjabooo/seele/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Tjabooo/seele?include_prereleases&sort=semver)](https://github.com/Tjabooo/seele/releases) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Prompt Claude Code on your own machine from Discord: DM the bot (or talk to it in a server
thread) and it runs a real Claude Code session there, streams what it's doing, and posts the
answer back. Check your plan usage, switch effort, model, folder and account, and pick up
sessions you started in the terminal. It also hosts long-running "lead" sessions that run
background agents for hours: talk to them while they work and watch every agent on its own card.

```
you   ▸ why is the API log full of 404s since this morning?
seele ▸ ⏳ Working · 0:41 · 6 tools · 5 h 12%
        -# ~/Projects/api · claude · default model · effort high · auto
        ▸ Bash: tail -200 logs/server.log
        ▸ Grep: 404
        · The 404s all come from the old /thumbs route…
seele ▸ ✅ Done · 1:07 · 9 tools · 5 h 13% · `3f2a91c0`
        The thumbnails moved to /img in yesterday's commit, but the mobile app still asks for /thumbs…
```

## Features

- **Prompt from anywhere.** DMs work out of the box. In a server, mention the bot (or turn on
  `/listen` in a channel) and every prompt gets its own thread, so each thread is its own
  Claude session.
- **Live progress.** One status message shows the elapsed time, tool calls and Claude's running
  commentary, with a **Stop** button.
- **Talk while it works.** A message sent while Claude is busy goes straight into the running
  session (📨): Claude reads it at its next step. Only once the run is wrapping up are messages
  queued (📥) for the next one.
- **Every answer as it comes.** A session that waits for background agents answers once per turn
  (when it starts them, when each one reports back); each answer is posted right away and the live
  status moves below it.
- **Agent cards.** Point a conversation at an agents channel (`/agents #channel`) and every
  background agent gets a live card there (status, elapsed, tools, tokens, what it's doing, a link
  back) with a thread holding its prompt, its steps and its final report. Without one, running
  agents are listed in the live status.
- **Auto-resume.** With `/autoresume on`, a run stopped by a usage limit (5-hour or weekly) is
  picked up again 5 minutes after the limit resets. The schedule survives restarts.
- **Scriptable.** `seele post`, `seele thread`, `seele conv`, `seele prompt` and `seele usage` let
  Claude itself (or any script) post screenshots, open threads, change settings, hand over to a
  fresh session and pace itself against plan limits.
- **Real sessions.** Conversations resume the same Claude Code session, and `/status` prints the
  `claude --resume …` line to carry on in a terminal. `/sessions` goes the other way: continue a
  terminal session from your phone.
- **Usage.** `/usage` shows the same session/weekly limits as Claude Code's `/usage`, for every
  account, as bars with reset times. `/cost` runs a spend report command if you have one.
- **Settings per conversation:** `/effort` (or save it as Claude Code's default with
  `everywhere:True`), `/model`, `/mode` (permission mode), `/account`, `/cd`, `/new`.
- **Several accounts.** Map folders to Claude accounts (`CLAUDE_CONFIG_DIR`), each with its own
  default effort. When one hits its limit, the reply comes with a **Retry on …** button.
- **Attachments.** Files and screenshots you send are saved locally and handed to Claude by path.
  `/get path` uploads a file back.
- **Guard process.** While a given process runs (say, a game), Claude is told to keep it light.
- **Owner only.** Everyone else is ignored; the owner defaults to the Discord application's owner.
- **Knows who's talking.** With several owners, every prompt starts with `[Discord message from NAME]`
  (names from `[people]` in the config, else the Discord display name), so Claude can tell them apart.

## Install

Needs Python 3.11+, a logged-in [Claude Code](https://docs.claude.com/en/docs/claude-code) CLI
and systemd (any Linux desktop/server).

1. **Create the bot.** At <https://discord.com/developers/applications>: *New Application* →
   *Bot* → *Reset Token* and copy it. On the same page, turn on **Message Content Intent** (only
   needed for `/listen` channels; DMs and mentions work without it). Under *Installation*, tick
   both **User Install** and **Guild Install**.
2. **Install on the machine that should run Claude:**
   ```sh
   git clone https://github.com/Tjabooo/seele && cd seele
   ./install.sh                         # venv in ~/.local/share/seele, user service, config skeleton
   $EDITOR ~/.config/seele/config.toml  # paste the token
   systemctl --user restart seele
   ```
3. **Add it.** `~/.local/share/seele/venv/bin/python -m seele invite` prints two links: one adds
   the bot to a server, the other to your own account (so you can DM it and use its commands
   anywhere). The bot DMs you when it first comes online.

To keep it running while you're logged out: `loginctl enable-linger $USER`.

## Usage

Just write to it. Everything else is slash commands:

| Command | What it does |
| --- | --- |
| `/new` | Fresh session next message (keeps folder and settings) |
| `/cd path` | Change the working folder; autocompletes your projects and paths |
| `/effort level [everywhere]` | low · medium · high · xhigh · max · default; `everywhere` saves it as Claude Code's default |
| `/model name` | opus, sonnet, haiku, a full model id, or default |
| `/mode mode` | Permission mode: auto · acceptEdits · plan · bypassPermissions |
| `/account name` | Which Claude account runs this conversation (or auto, by folder) |
| `/usage` | Plan limits for every account |
| `/cost [days]` | Run your spend report command |
| `/status` | Folder, account, model, effort, session id and the terminal resume line |
| `/sessions` | Pick a recent session in this folder to continue |
| `/stop` | Stop the running prompt, drop queued messages and any scheduled auto-resume |
| `/agents [channel]` | Post a live card per background agent in that channel (empty = list them in the status) |
| `/autoresume on` | After a usage limit, carry on automatically when it resets |
| `/get path` | Upload a file from the machine |
| `/listen on` | In a server channel: every message becomes a prompt (needs Message Content Intent) |

Settings stick per DM, channel or thread; a new thread starts with its channel's settings
(including its agents channel and auto-resume).

### Command line

The `seele` command (linked into `~/.local/bin` by `install.sh`) works next to the running bot.
Channel arguments take an id or a name from `[channels]` in the config.

```sh
seele post --channel progress --file shot.png --file clip.mp4 "The reef scene is in"
seele post --channel 123 --reply-to 456 -          # text from stdin; prints the message id(s)
seele thread --channel lead --name "Sprint 4"      # prints the new thread's id
seele conv --id 789 --cwd ~/Projects/game --agents-channel agents --autoresume on --effort xhigh
seele prompt --id 789 --file handover.md --new-session --as lead
seele usage --json                                 # 5-hour + weekly utilization (0-1) and resets
```

- **`post`**: message as the bot via Discord's REST API: up to 10 files (each at most
  `max_upload_mb`), long text split into several messages, files on the last one.
- **`thread`**: a public thread in a text channel; prints its id.
- **`conv`**: change a conversation's settings: `--cwd`, `--account`, `--effort`, `--model`,
  `--mode`, `--session SID|new`, `--agents-channel ID|off`, `--autoresume on|off`, `--listen on|off`.
- **`prompt`**: run a prompt in a conversation **after its current run ends** (`--text` or
  `--file`, `--as NAME` tags it like a message from NAME). `--new-session` starts it in a fresh
  Claude session: a long-running session hands over by calling this and then ending its turn with
  no agents left, so its process exits and the fresh one starts.
- **`usage`**: per account, `five_hour` and `seven_day` utilization (0–1) with reset times, from
  the same source as `/usage` (`--account NAME`, `--json`).

`conv` and `prompt` don't talk to Discord: they drop a request file into
`~/.local/state/seele/requests/` (next to the state file), the bot applies it within ~3 s and
writes an answer the command waits for (`--wait SECONDS`, default 15). The bot owns the state
file, so settings always change through it.

## Lead mode

For a long-running session that coordinates background agents (a "lead"):

1. Make a channel for the lead, one for agent cards, and optionally one for progress posts; name
   them in `[channels]`. Give the lead a thread (in a server channel every new prompt otherwise
   gets its own thread, so its own session): `seele thread --channel lead --name Lead`.
2. `seele conv --id THREAD_ID --cwd ~/Projects/x --agents-channel agents --autoresume on`, then
   talk to it in that thread. Everyone listed in `owners` can talk to it; each message is tagged
   with who sent it.
3. The lead starts agents with `run_in_background`; its process stays alive while they work and
   each completion wakes it for another turn. Messages you send meanwhile reach it right away.
4. The run ends when the lead's turn is over, nothing runs in the background and nobody wrote in
   since. The next message starts a new run in the same session.

`SEELE_MAX_PARALLEL` (default 3) counts a lead as one running prompt for as long as it lives.

## Configuration

`~/.config/seele/config.toml` (see [`config.example.toml`](config.example.toml)):

```toml
token = "…"                 # or DISCORD_TOKEN in the environment
owners = []                 # Discord user ids; empty = the application owner
guilds = []                 # servers whose slash commands should update instantly

[claude]
default_cwd = "~"
projects_root = "~/Projects"
permission_mode = "auto"
effort = ""                 # empty = Claude Code's saved default

[claude.env]                # extra environment for Claude Code runs
NOTIFY_DONE = "0"

[accounts.work]
config_dir = "~/.claude"

[accounts.side]
config_dir = "~/.claude-2"
paths = ["~/Projects/side-project"]
effort = "xhigh"

[guard]
process = "steam_game"
note = "A game is running: no builds or test suites unless asked."

[channels]                  # names for the CLI's --channel / --id / --agents-channel
progress = 123456789012345678
```

`[claude] live_input = false` turns live messages off (everything sent while Claude works is
queued for the next run, as in 0.1).

Other environment variables: `SEELE_CONFIG`, `SEELE_STATE` (default
`~/.local/state/seele/state.json`), `SEELE_MAX_PARALLEL` (prompts running at once, default 3).

## How it works

Each prompt runs `claude -p --input-format stream-json --output-format stream-json --verbose
--resume <session>` in the conversation's folder with its effort, model and permission mode. The
prompt goes in as a stream-json user message and stdin stays open, so later messages can follow
into the same process. The bot reads the JSON events (init → tool calls → text → rate-limit info →
result, plus `task_*` and `background_tasks_changed` events for background work and the subagents'
own steps, marked with `parent_tool_use_id`), edits the status message every couple of seconds,
and posts each result split into Discord-sized pieces (long replies come as a file). Once a turn
has ended, no background task is left and nothing new was written, it closes stdin and Claude
Code exits. A short system-prompt addition tells Claude its reply is read on Discord and that it
can't ask interactive questions.

Agent cards are tracked by task id in the state file, so an agent that is resumed later (or by a
new run in the same session) keeps its card and thread. Discord limits are respected by batching:
a card is edited at most every 10 s, and an agent's steps go to its thread in one message every
~12 s.

`/usage` reads the account's OAuth token from `.credentials.json` and asks the same endpoint
Claude Code's `/usage` uses; if the token has expired it runs `claude -p /usage` instead, which
refreshes the login.

## Security

Anyone who controls the owner's Discord account can run Claude Code, and so commands, on the
machine. Keep the default `auto` permission mode (Claude Code's own safety classifier vets each
action), use 2FA on Discord, and treat the bot token like a password (`chmod 600` the config).
Messages from anyone else are ignored without a reply. The requests folder is as powerful as the
owner (a `seele prompt` runs Claude Code): it's created `0700` and the bot ignores files that
belong to another user.

## License

MIT
