"""Settings file (TOML) and the per-conversation state file (JSON)."""
from __future__ import annotations

import json
import os
import socket
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .hosts import Host

CONFIG_PATH = Path(os.environ.get("SEELE_CONFIG", "~/.config/seele/config.toml")).expanduser()
STATE_PATH = Path(os.environ.get("SEELE_STATE", "~/.local/state/seele/state.json")).expanduser()
REQUESTS_DIR = Path(os.environ.get("SEELE_REQUESTS", STATE_PATH.parent / "requests")).expanduser()
MAX_AGENT_CARDS = 400  # agent cards remembered in the state file (oldest dropped)

DEFAULT_SYSTEM_PROMPT = (
    "You are running headless: people are talking to you through a Discord bot and read your "
    "final reply on Discord, often on a phone. Keep the final reply short and use Discord markdown "
    "(no wide tables, no HTML). You cannot ask interactive questions; if you need a decision, ask "
    "it at the end of your reply and they will answer in the next message."
)

SPEAKER_NOTE = (
    "Each Discord message starts with a line like `[Discord message from NAME]` saying who sent it. "
    "More than one person may use this bot, even in the same conversation: reply to the person who "
    "sent the latest message, and don't assume they are the machine's owner unless the name says so."
)


def speaker_line(name: str) -> str:
    return f"[Discord message from {name}]"


def tag_prompt(name: str, prompt: str) -> str:
    """Prefix a prompt with who sent it, so Claude can tell users apart."""
    return f"{speaker_line(name)}\n{prompt}"


@dataclass
class Account:
    name: str
    config_dir: Path
    paths: list[Path] = field(default_factory=list)  # working dirs that pick this account
    effort: str = ""  # default effort for runs on this account
    config_dir_raw: str = ""  # as written in the config (~ unexpanded): for other hosts
    paths_raw: list[str] = field(default_factory=list)
    usage_host: str = ""  # host whose login /usage reads (empty: here if logged in, else the default host)

    def config_dir_on(self, host: Host) -> str:
        return str(self.config_dir) if host.local else host.path(self.config_dir_raw or "~/.claude")

    def is_default_dir(self, host: Host) -> bool:
        """The host's own ~/.claude (then CLAUDE_CONFIG_DIR stays unset, which keeps ~/.claude.json)."""
        if host.local:
            return self.config_dir.resolve() == (Path.home() / ".claude").resolve()
        return self.config_dir_on(host) == host.path("~/.claude")


@dataclass
class Config:
    token: str = ""
    owners: set[int] = field(default_factory=set)
    people: dict[int, str] = field(default_factory=dict)  # Discord user id -> name Claude sees
    guilds: list[int] = field(default_factory=list)
    claude_bin: str = "claude"
    projects_root: Path = Path("~/Projects").expanduser()
    default_cwd: Path = Path.home()
    model: str = ""
    effort: str = ""
    permission_mode: str = "auto"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    env: dict[str, str] = field(default_factory=dict)
    accounts: list[Account] = field(default_factory=list)
    guard_process: str = ""
    guard_note: str = ""
    cost_command: str = ""
    settings_file: Path = Path("~/.claude/settings.json").expanduser()
    max_upload_mb: int = 10
    channels: dict[str, int] = field(default_factory=dict)  # names for `seele post --channel NAME`
    live_input: bool = True  # messages sent while Claude works go straight into the running turn
    local_host: str = ""  # this machine's name among the hosts
    default_host: str = ""  # where conversations run unless /host says otherwise
    hosts: dict[str, Host] = field(default_factory=dict)
    bot_ssh: str = ""  # [bot] ssh: the bot runs on that machine; seele conv/prompt hand requests to it
    bot_command: str = "seele"  # [bot] command: the seele CLI there

    def host(self, name: str = "") -> Host:
        """A host by name ("" = the default host). Unknown names fall back to the default."""
        if not self.hosts:
            self.hosts[self.local_host or "local"] = Host(self.local_host or "local",
                                                         default_cwd=str(self.default_cwd),
                                                         projects_root=str(self.projects_root))
        return (self.hosts.get(name) or self.hosts.get(self.default_host)
                or self.hosts.get(self.local_host) or next(iter(self.hosts.values())))

    def name_for(self, uid: int, fallback: str) -> str:
        return self.people.get(uid) or fallback

    def channel_id(self, value: str | int) -> int:
        """A channel id from an id, a `<#id>` mention or a name in `[channels]`."""
        text = str(value).strip().removeprefix("<#").removesuffix(">").lstrip("#")
        if text.isdigit():
            return int(text)
        if text in self.channels:
            return self.channels[text]
        known = ", ".join(sorted(self.channels)) or "none configured"
        raise ValueError(f"unknown channel {value!r} (names in [channels]: {known})")

    def user_id(self, value: str | int) -> int:
        """A Discord user id from an id, a `<@id>` mention or a name in `[people]` (case-insensitive)."""
        text = str(value).strip().removeprefix("<@").removeprefix("!").removesuffix(">").lstrip("@")
        if text.isdigit():
            return int(text)
        for uid, name in self.people.items():
            if name.lower() == text.lower():
                return uid
        known = ", ".join(sorted(self.people.values())) or "none configured"
        raise ValueError(f"unknown person {value!r} (names in [people]: {known})")

    def account_for(self, cwd, override: str | None = None, host: Host | None = None) -> Account:
        if override:
            for acc in self.accounts:
                if acc.name == override:
                    return acc
        if host is not None and not host.local:
            for acc in self.accounts:
                if any(host.contains(host.path(p), str(cwd)) for p in acc.paths_raw):
                    return acc
            return self.accounts[0]
        cwd = Path(cwd).resolve()
        for acc in self.accounts:
            for p in acc.paths:
                if cwd == p or p in cwd.parents:
                    return acc
        return self.accounts[0]


def account_env(cfg: "Config", account: Account) -> dict[str, str]:
    """Environment for Claude Code runs on `account`."""
    env = {**os.environ, **cfg.env}
    if account.config_dir.resolve() == (Path.home() / ".claude").resolve():
        env.pop("CLAUDE_CONFIG_DIR", None)  # default dir keeps ~/.claude.json
    else:
        env["CLAUDE_CONFIG_DIR"] = str(account.config_dir)
    return env


def host_env(cfg: "Config", account: Account, host: Host) -> tuple[dict[str, str], tuple[str, ...]]:
    """(variables to set, variables to unset) for Claude Code runs on `account` on `host`."""
    env = dict(cfg.env)
    if account.is_default_dir(host):
        return env, ("CLAUDE_CONFIG_DIR",)
    env["CLAUDE_CONFIG_DIR"] = account.config_dir_on(host)
    return env, ()


def _path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def load(path: Path = CONFIG_PATH) -> Config:
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    cfg = Config()
    cfg.token = os.environ.get("DISCORD_TOKEN") or raw.get("token", "")
    cfg.owners = {int(x) for x in raw.get("owners", [])}
    cfg.people = {int(k): str(v) for k, v in raw.get("people", {}).items()}
    cfg.guilds = [int(x) for x in raw.get("guilds", [])]
    c = raw.get("claude", {})
    cfg.claude_bin = c.get("binary", cfg.claude_bin)
    cfg.projects_root = _path(c.get("projects_root", str(cfg.projects_root)))
    cfg.default_cwd = _path(c.get("default_cwd", str(cfg.default_cwd)))
    cfg.model = c.get("model", "")
    cfg.effort = c.get("effort", "")
    cfg.permission_mode = c.get("permission_mode", cfg.permission_mode)
    cfg.system_prompt = c.get("system_prompt", cfg.system_prompt)
    cfg.env = {str(k): str(v) for k, v in c.get("env", {}).items()}
    cfg.settings_file = _path(c.get("settings_file", str(cfg.settings_file)))
    cfg.cost_command = raw.get("cost_command", "")
    cfg.max_upload_mb = int(raw.get("max_upload_mb", cfg.max_upload_mb))
    cfg.channels = {str(k): int(v) for k, v in raw.get("channels", {}).items()}
    cfg.live_input = bool(c.get("live_input", cfg.live_input))
    for name, acc in raw.get("accounts", {}).items():
        cfg.accounts.append(Account(name, _path(acc.get("config_dir", "~/.claude")),
                                    [_path(p).resolve() for p in acc.get("paths", [])],
                                    acc.get("effort", ""), acc.get("config_dir", "~/.claude"),
                                    [str(p) for p in acc.get("paths", [])], acc.get("usage_host", "")))
    if not cfg.accounts:
        default = os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")
        cfg.accounts.append(Account("claude", _path(default)))
    cfg.local_host = str(raw.get("host_name") or socket.gethostname().split(".")[0].lower() or "local")
    cfg.hosts[cfg.local_host] = Host(cfg.local_host, default_cwd=str(cfg.default_cwd),
                                     projects_root=str(cfg.projects_root))
    ssh = raw.get("ssh", {})
    for name, h in raw.get("hosts", {}).items():
        if name == cfg.local_host and not h.get("ssh"):
            continue  # this machine: [claude] has its settings
        cfg.hosts[name] = Host(name, ssh=h.get("ssh", ""), mac=h.get("mac", ""),
                               broadcast=h.get("broadcast", "255.255.255.255"), home=h.get("home", ""),
                               claude_bin=h.get("binary", ""), default_cwd=h.get("default_cwd", "~"),
                               projects_root=h.get("projects_root", "~/Projects"),
                               wake_timeout=float(h.get("wake_timeout", 240)),
                               ssh_bin=h.get("ssh_binary", ssh.get("binary", "")),
                               ssh_opts=tuple(str(x) for x in h.get("ssh_options", ssh.get("options", []))))
    cfg.default_host = str(raw.get("default_host") or cfg.local_host)
    if cfg.default_host not in cfg.hosts:
        raise ValueError(f"default_host {cfg.default_host!r} is not in [hosts] (have: {', '.join(cfg.hosts)})")
    b = raw.get("bot", {})
    cfg.bot_ssh = b.get("ssh", "")
    cfg.bot_command = b.get("command", cfg.bot_command)
    g = raw.get("guard", {})
    cfg.guard_process = g.get("process", "")
    cfg.guard_note = g.get("note", "")
    return cfg


@dataclass
class Conversation:
    """Settings for one DM, channel or thread. Empty strings mean "use the default"."""
    session_id: str = ""
    cwd: str = ""
    host: str = ""  # which machine runs Claude ("" = the default host)
    account: str = ""
    model: str = ""
    effort: str = ""
    mode: str = ""
    listen: bool = False  # server channels: answer every message, not only mentions
    last_prompt: str = ""
    agents_channel: str = ""  # channel id for subagent cards; empty = list them in the status
    autoresume: bool = False  # after a usage limit or an outage, prompt again once it has passed
    resume_at: float = 0.0  # epoch seconds: the pending auto-resume goes out no earlier (0 = no time set)
    resume_guessed: bool = False  # resume_at is a guess (the reset time was unknown)
    # background agents a limit or an outage stopped, named in the next auto-resume prompt:
    # [{"id": agent/task id (what SendMessage takes), "description": ..., "type": subagent type,
    #   "cause": "limit" | "network" | "server"}]
    resume_agents: list = field(default_factory=list)
    # what the pending auto-resume waits for: "limit" (resume_at), "network"/"server" (the API answering)
    resume_kinds: list = field(default_factory=list)
    resume_since: float = 0.0  # when the current outage was first seen (for "network outage 04:02–04:32")
    resume_tries: int = 0  # auto-resumes in a row that failed again on an outage (backoff)
    resume_last: float = 0.0  # when the last auto-resume went out
    resume_sent: list = field(default_factory=list)  # the agents it named, until a turn goes through
    light: str = ""  # threads: the 🟠/🟢/🔴 their name shows (see lights.py)

    def inherit(self) -> "Conversation":
        """Settings a new thread takes from its channel (no session)."""
        return Conversation(cwd=self.cwd, host=self.host, account=self.account, model=self.model,
                            effort=self.effort, mode=self.mode, agents_channel=self.agents_channel,
                            autoresume=self.autoresume)


class State:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.convs: dict[str, Conversation] = {}
        self.agents: dict[str, dict] = {}  # task id -> agent card (see agents.Card)
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                fields = Conversation.__dataclass_fields__
                for key, val in raw.get("conversations", {}).items():
                    self.convs[key] = Conversation(**{k: v for k, v in val.items() if k in fields})
                self.agents = {str(k): v for k, v in (raw.get("agents") or {}).items() if isinstance(v, dict)}
            except (OSError, json.JSONDecodeError, TypeError, AttributeError):
                pass

    def get(self, channel_id: int) -> Conversation:
        return self.convs.setdefault(str(channel_id), Conversation())

    def has(self, channel_id: int) -> bool:
        return str(channel_id) in self.convs

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if len(self.agents) > MAX_AGENT_CARDS:
            keep = sorted(self.agents.items(), key=lambda kv: kv[1].get("started", 0))[-MAX_AGENT_CARDS:]
            self.agents = dict(keep)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"conversations": {k: asdict(v) for k, v in self.convs.items()},
                                   "agents": self.agents}, indent=1))
        tmp.replace(self.path)


def apply_effort(data: dict, level: str) -> list[str]:
    """Set `level` as the default effort in a Claude Code settings dict. Returns the models changed."""
    models = data.setdefault("modelSettings", {})
    if not models:
        data["effortLevel"] = level
        return ["(all)"]
    for name in models:
        models[name]["effortLevel"] = level
    return list(models)


def effort_in(data: dict) -> str:
    for val in (data.get("modelSettings") or {}).values():
        if isinstance(val, dict) and val.get("effortLevel"):
            return val["effortLevel"]
    return data.get("effortLevel", "")


def set_default_effort(settings_file: Path, level: str) -> list[str]:
    """Save `level` as Claude Code's default effort (what /effort does). Returns the models changed."""
    real = settings_file.resolve()  # the file may be a symlink into a synced folder
    data = json.loads(real.read_text()) if real.exists() else {}
    changed = apply_effort(data, level)
    tmp = real.with_name(real.name + ".seele-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(real)
    return changed


def default_effort(settings_file: Path) -> str:
    try:
        data = json.loads(settings_file.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    return effort_in(data)
