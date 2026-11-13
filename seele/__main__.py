"""seele: prompt Claude Code from Discord.

  python -m seele            run the bot
  python -m seele invite     print the links that add the bot to a server or to your account
  python -m seele check      check the config and the claude binary
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import sys
from pathlib import Path

from . import config

PERMISSIONS = (
    1 << 6      # add reactions
    | 1 << 10   # view channels
    | 1 << 11   # send messages
    | 1 << 14   # embed links
    | 1 << 15   # attach files
    | 1 << 16   # read message history
    | 1 << 35   # create public threads
    | 1 << 38   # send messages in threads
)


def app_id_from_token(token: str) -> str:
    """A bot token starts with the bot's user id (= application id) in base64."""
    head = token.split(".")[0]
    return base64.b64decode(head + "=" * (-len(head) % 4)).decode()


def invite_links(app_id: str) -> tuple[str, str]:
    base = f"https://discord.com/oauth2/authorize?client_id={app_id}"
    server = f"{base}&scope=bot+applications.commands&permissions={PERMISSIONS}&integration_type=0"
    user = f"{base}&scope=applications.commands&integration_type=1"
    return server, user


def ensure_path():
    local_bin = str(Path.home() / ".local" / "bin")
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if local_bin not in parts:
        os.environ["PATH"] = os.pathsep.join([local_bin, *parts])


def main(argv: list[str]) -> int:
    ensure_path()
    cfg = config.load()
    cmd = argv[0] if argv else "run"
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if not cfg.token:
        print(f"No Discord bot token. Put token = \"...\" in {config.CONFIG_PATH} "
              "(see config.example.toml) or set DISCORD_TOKEN.", file=sys.stderr)
        return 2
    if cmd == "invite":
        server, user = invite_links(app_id_from_token(cfg.token))
        print(f"Add to a server:\n  {server}\n\nAdd to your account (DMs and commands anywhere):\n  {user}")
        return 0
    if cmd == "check":
        ok = True
        binary = shutil.which(cfg.claude_bin)
        print(f"claude binary: {binary or 'NOT FOUND'}")
        ok &= bool(binary)
        for acc in cfg.accounts:
            creds = (acc.config_dir / ".credentials.json").exists()
            print(f"account {acc.name}: {acc.config_dir} ({'logged in' if creds else 'no .credentials.json'})")
        print(f"default folder: {cfg.default_cwd} ({'ok' if cfg.default_cwd.is_dir() else 'MISSING'})")
        print(f"projects root: {cfg.projects_root}")
        print(f"owners: {sorted(cfg.owners) or 'the application owner'}")
        print(f"application id: {app_id_from_token(cfg.token)}")
        return 0 if ok else 1
    if cmd != "run":
        print(__doc__, file=sys.stderr)
        return 2

    import discord

    from .bot import Seele
    discord.utils.setup_logging(level=logging.INFO)
    for content in (True, False):
        bot = Seele(cfg, config.State(), message_content=content)
        try:
            bot.run(cfg.token, log_handler=None)
            return 0
        except discord.PrivilegedIntentsRequired:
            logging.getLogger("seele").warning(
                "Message Content intent is off in the Developer Portal: DMs and mentions still work, "
                "/listen channels don't. Turning it on fixes that.")
    return 1


def cli():
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
