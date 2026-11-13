"""Settings file (TOML) and the per-conversation state file (JSON)."""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("SEELE_CONFIG", "~/.config/seele/config.toml")).expanduser()
STATE_PATH = Path(os.environ.get("SEELE_STATE", "~/.local/state/seele/state.json")).expanduser()

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

    def name_for(self, uid: int, fallback: str) -> str:
        return self.people.get(uid) or fallback

    def account_for(self, cwd: Path, override: str | None = None) -> Account:
        if override:
            for acc in self.accounts:
                if acc.name == override:
                    return acc
        cwd = cwd.resolve()
        for acc in self.accounts:
            for p in acc.paths:
                if cwd == p or p in cwd.parents:
                    return acc
        return self.accounts[0]


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
    for name, acc in raw.get("accounts", {}).items():
        cfg.accounts.append(Account(name, _path(acc.get("config_dir", "~/.claude")),
                                    [_path(p).resolve() for p in acc.get("paths", [])],
                                    acc.get("effort", "")))
    if not cfg.accounts:
        default = os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")
        cfg.accounts.append(Account("claude", _path(default)))
    g = raw.get("guard", {})
    cfg.guard_process = g.get("process", "")
    cfg.guard_note = g.get("note", "")
    return cfg


@dataclass
class Conversation:
    """Settings for one DM, channel or thread. Empty strings mean "use the default"."""
    session_id: str = ""
    cwd: str = ""
    account: str = ""
    model: str = ""
    effort: str = ""
    mode: str = ""
    listen: bool = False  # server channels: answer every message, not only mentions
    last_prompt: str = ""

    def inherit(self) -> "Conversation":
        """Settings a new thread takes from its channel (no session)."""
        return Conversation(cwd=self.cwd, account=self.account, model=self.model,
                            effort=self.effort, mode=self.mode)


class State:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.convs: dict[str, Conversation] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                fields = Conversation.__dataclass_fields__
                for key, val in raw.get("conversations", {}).items():
                    self.convs[key] = Conversation(**{k: v for k, v in val.items() if k in fields})
            except (OSError, json.JSONDecodeError, TypeError):
                pass

    def get(self, channel_id: int) -> Conversation:
        return self.convs.setdefault(str(channel_id), Conversation())

    def has(self, channel_id: int) -> bool:
        return str(channel_id) in self.convs

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"conversations": {k: asdict(v) for k, v in self.convs.items()}},
                                  indent=1))
        tmp.replace(self.path)


def set_default_effort(settings_file: Path, level: str) -> list[str]:
    """Save `level` as Claude Code's default effort (what /effort does). Returns the models changed."""
    real = settings_file.resolve()  # the file may be a symlink into a synced folder
    data = json.loads(real.read_text()) if real.exists() else {}
    models = data.setdefault("modelSettings", {})
    if not models:
        data["effortLevel"] = level
        changed = ["(all)"]
    else:
        for name in models:
            models[name]["effortLevel"] = level
        changed = list(models)
    tmp = real.with_name(real.name + ".seele-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(real)
    return changed


def default_effort(settings_file: Path) -> str:
    try:
        data = json.loads(settings_file.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    for val in (data.get("modelSettings") or {}).values():
        if isinstance(val, dict) and val.get("effortLevel"):
            return val["effortLevel"]
    return data.get("effortLevel", "")
