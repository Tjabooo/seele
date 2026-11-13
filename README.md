# seele

[![CI](https://github.com/Tjabooo/seele/actions/workflows/ci.yml/badge.svg)](https://github.com/Tjabooo/seele/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Tjabooo/seele?include_prereleases&sort=semver)](https://github.com/Tjabooo/seele/releases) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Prompt Claude Code on your own machine from Discord: DM the bot (or talk to it in a server
thread) and it runs a real Claude Code session there, streams what it's doing, and posts the
answer back. Check your plan usage, switch effort, model, folder and account, and pick up
sessions you started in the terminal.

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
  commentary, with a **Stop** button. Messages sent while it's busy are queued and sent together.
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
| `/stop` | Stop the running prompt and drop queued messages |
| `/get path` | Upload a file from the machine |
| `/listen on` | In a server channel: every message becomes a prompt (needs Message Content Intent) |

Settings stick per DM, channel or thread; a new thread starts with its channel's settings.

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
```

Other environment variables: `SEELE_CONFIG`, `SEELE_STATE` (default
`~/.local/state/seele/state.json`), `SEELE_MAX_PARALLEL` (prompts running at once, default 3).

## How it works

Each prompt runs `claude -p --output-format stream-json --verbose --resume <session>` in the
conversation's folder with its effort, model and permission mode, prompt on stdin. The bot reads
the JSON events (init → tool calls → text → rate-limit info → result), edits the status message
every couple of seconds, and posts the result split into Discord-sized pieces (long replies come
as a file). A short system-prompt addition tells Claude its reply is read on Discord and that it
can't ask interactive questions.

`/usage` reads the account's OAuth token from `.credentials.json` and asks the same endpoint
Claude Code's `/usage` uses; if the token has expired it runs `claude -p /usage` instead, which
refreshes the login.

## Security

Anyone who controls the owner's Discord account can run Claude Code, and so commands, on the
machine. Keep the default `auto` permission mode (Claude Code's own safety classifier vets each
action), use 2FA on Discord, and treat the bot token like a password (`chmod 600` the config).
Messages from anyone else are ignored without a reply.

## License

MIT
