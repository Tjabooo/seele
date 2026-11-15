"""seele: prompt Claude Code from Discord.

  seele [run]       run the bot
  seele invite      print the links that add the bot to a server or to your account
  seele check       check the config and the claude binary
  seele post --channel NAME_OR_ID [--file PATH ...] [--reply-to MSG_ID] [TEXT | -]
                    post a message (and up to 10 files) as the bot; prints the message id
  seele thread --channel NAME_OR_ID --name NAME
                    create a public thread in a channel; prints its id
  seele history (--channel NAME_OR_ID | --server GUILD_ID) [--limit N] [--bots] [--json]
                    print a channel's messages (or every channel and thread in a server),
                    oldest first; --limit counts per channel, bots are left out unless --bots
  seele conv --id ID [--cwd P] [--account A] [--effort E] [--model M] [--mode M] [--session SID|new]
             [--agents-channel ID|off] [--autoresume on|off] [--listen on|off]
                    change a conversation's settings (the running bot applies them)
  seele prompt --id ID [--new-session] (--text T | --file F) [--as NAME]
                    prompt a conversation once its current run has ended
  seele usage [--account NAME] [--json]
                    five-hour and weekly utilization (0-1) and reset times per account

Channel names come from [channels] in the config. `conv` and `prompt` go through the requests
folder next to the state file; they wait (--wait seconds, default 15) for the bot's answer.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from . import claude, config

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


def write_request(data: dict, folder: Path | None = None) -> Path:
    """Drop a request in the bot's spool (atomically: written aside, then renamed)."""
    folder = folder or config.REQUESTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    name = f"{time.time_ns()}-{os.getpid()}"
    tmp = folder / f".{name}.tmp"
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    final = folder / f"{name}.json"
    tmp.replace(final)
    return final


def wait_answer(path: Path, timeout: float) -> dict | None:
    done = path.with_suffix(".done")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done.exists():
            try:
                answer = json.loads(done.read_text())
            except (OSError, json.JSONDecodeError):
                answer = None
            if answer is not None:
                done.unlink(missing_ok=True)
                return answer
        time.sleep(0.25)
    return None


def submit(data: dict, wait: float) -> int:
    path = write_request(data)
    if wait <= 0:
        print(f"queued {path.name}")
        return 0
    answer = wait_answer(path, wait)
    if answer is None:
        print(f"queued {path.name}, but SEELE didn't pick it up within {wait:g} s (is the bot running?). "
              "It applies when the bot next looks.", file=sys.stderr)
        return 0
    print(answer.get("message", ""), file=sys.stdout if answer.get("ok") else sys.stderr)
    return 0 if answer.get("ok") else 1


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="seele", description="Prompt Claude Code from Discord.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the bot")
    sub.add_parser("invite", help="print the links that add the bot")
    sub.add_parser("check", help="check the config and the claude binary")

    p = sub.add_parser("post", help="post a message (and files) as the bot")
    p.add_argument("--channel", required=True, help="channel or thread id, or a name from [channels]")
    p.add_argument("--file", action="append", default=[], metavar="PATH", help="attach a file (up to 10)")
    p.add_argument("--reply-to", type=int, metavar="MSG_ID", help="reply to this message")
    p.add_argument("text", nargs="?", default="", help="message text; - reads it from stdin")

    p = sub.add_parser("thread", help="create a public thread and print its id")
    p.add_argument("--channel", required=True, help="text channel id, or a name from [channels]")
    p.add_argument("--name", required=True)

    on_off = ("on", "off")
    p = sub.add_parser("history", help="print a channel's (or a whole server's) messages")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--channel", help="channel or thread id, or a name from [channels]")
    where.add_argument("--server", type=int, metavar="GUILD_ID", help="every channel and thread in this server")
    p.add_argument("--limit", type=int, metavar="N", help="newest N messages per channel (default: all)")
    p.add_argument("--bots", action="store_true", help="include messages from bots")
    p.add_argument("--json", action="store_true", help="raw Discord message objects, one per line")

    p = sub.add_parser("conv", help="change a conversation's settings")
    p.add_argument("--id", required=True, help="thread / channel / DM id, or a name from [channels]")
    p.add_argument("--cwd", help="working folder (a project name, ~/path or /path)")
    p.add_argument("--account", help="account name from the config, or auto")
    p.add_argument("--effort", choices=(*claude.EFFORTS, "default"))
    p.add_argument("--model", help="model id or alias, or default")
    p.add_argument("--mode", choices=(*claude.MODES, "default"), help="permission mode")
    p.add_argument("--session", metavar="SID", help="session id to continue, or new")
    p.add_argument("--agents-channel", metavar="ID", help="channel for agent cards, or off")
    p.add_argument("--autoresume", choices=on_off)
    p.add_argument("--listen", choices=on_off)
    p.add_argument("--wait", type=float, default=15, help="seconds to wait for the bot's answer (0 = don't)")

    p = sub.add_parser("prompt", help="prompt a conversation once its current run has ended")
    p.add_argument("--id", required=True, help="thread / channel / DM id, or a name from [channels]")
    p.add_argument("--new-session", action="store_true", help="start a fresh Claude session for it")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text")
    src.add_argument("--file", help="read the prompt from a file (- = stdin)")
    p.add_argument("--as", dest="speaker", metavar="NAME", help="tag it like a Discord message from NAME")
    p.add_argument("--wait", type=float, default=15, help="seconds to wait for the bot's answer (0 = don't)")

    p = sub.add_parser("usage", help="five-hour and weekly utilization per account")
    p.add_argument("--account", help="only this account (a name from the config)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def usage_report(cfg: config.Config, only: str | None, as_json: bool) -> int:
    import asyncio

    from . import usage
    accounts = [a for a in cfg.accounts if not only or a.name == only]
    if not accounts:
        print(f"seele usage: unknown account {only!r} (have: {', '.join(a.name for a in cfg.accounts)})",
              file=sys.stderr)
        return 2

    async def gather():
        return await asyncio.gather(*[usage.account_windows(cfg.claude_bin, a.config_dir, config.account_env(cfg, a))
                                      for a in accounts])
    found = dict(zip((a.name for a in accounts), asyncio.run(gather())))
    if as_json:
        print(json.dumps(found, indent=1))
    else:
        for name, info in found.items():
            if "error" in info:
                print(f"{name}: {info['error']}")
                continue
            parts = []
            for key, label in (("five_hour", "5 h"), ("seven_day", "week")):
                win = info.get(key)
                if win:
                    when = time.strftime("%a %b %d %H:%M", time.localtime(win["resets_epoch"])) if win["resets_epoch"] else "?"
                    parts.append(f"{label} {win['utilization'] * 100:.0f}% (resets {when})")
            print(f"{name}: " + " · ".join(parts))
    return 1 if any("error" in info for info in found.values()) else 0


def history(cfg: config.Config, args, *, opener=None) -> int:
    from . import rest
    kw = {"opener": opener} if opener else {}
    try:
        if args.server:
            chans = rest.guild_channels(cfg.token, args.server, **kw)
        else:
            chans = [{"id": cfg.channel_id(args.channel), "name": ""}]
        for chan in chans:
            try:
                msgs = rest.messages(cfg.token, int(chan["id"]), args.limit, **kw)
            except rest.RestError as e:
                if not args.server:
                    raise
                print(f"seele history: #{chan.get('name') or chan['id']}: {e}", file=sys.stderr)
                continue
            msgs = [m for m in reversed(msgs) if args.bots or not (m.get("author") or {}).get("bot")]
            if not msgs:
                continue
            if args.server and not args.json:
                print(f"\n## #{chan.get('name') or chan['id']}")
            for m in msgs:
                print(json.dumps(m) if args.json else rest.format_message(m, cfg.name_for))
        return 0
    except (ValueError, rest.RestError) as e:
        print(f"seele history: {e}", file=sys.stderr)
        return 1


def main(argv: list[str]) -> int:
    ensure_path()
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    args = parser().parse_args(argv or ["run"])
    cmd = args.cmd or "run"
    cfg = config.load()
    if cmd == "usage":
        return usage_report(cfg, args.account, args.json)

    if cmd in ("conv", "prompt"):
        try:
            cid = cfg.channel_id(args.id)
        except ValueError as e:
            print(f"seele: {e}", file=sys.stderr)
            return 2
        if cmd == "conv":
            changes = {k: v for k, v in {
                "cwd": args.cwd, "account": args.account, "effort": args.effort, "model": args.model,
                "mode": args.mode, "session": args.session, "agents_channel": args.agents_channel,
                "autoresume": args.autoresume, "listen": args.listen}.items() if v is not None}
            if not changes:
                print("seele conv: nothing to change (see seele conv --help)", file=sys.stderr)
                return 2
            if "agents_channel" in changes and changes["agents_channel"].lower() not in ("off", "none", ""):
                try:
                    changes["agents_channel"] = str(cfg.channel_id(changes["agents_channel"]))
                except ValueError as e:
                    print(f"seele: {e}", file=sys.stderr)
                    return 2
            return submit({"kind": "conv", "id": cid, "set": changes}, args.wait)
        if args.text is not None:
            text = args.text
        elif args.file == "-":
            text = sys.stdin.read()
        else:
            try:
                text = Path(args.file).expanduser().read_text()
            except OSError as e:
                print(f"seele prompt: {e}", file=sys.stderr)
                return 2
        if not text.strip():
            print("seele prompt: the prompt is empty", file=sys.stderr)
            return 2
        return submit({"kind": "prompt", "id": cid, "text": text, "new_session": args.new_session,
                       "as": args.speaker or ""}, args.wait)

    if not cfg.token:
        print(f"No Discord bot token. Put token = \"...\" in {config.CONFIG_PATH} "
              "(see config.example.toml) or set DISCORD_TOKEN.", file=sys.stderr)
        return 2
    if cmd == "history":
        return history(cfg, args)
    if cmd in ("post", "thread"):
        from . import rest
        try:
            cid = cfg.channel_id(args.channel)
            if cmd == "thread":
                print(rest.create_thread(cfg.token, cid, args.name)["id"])
                return 0
            text = sys.stdin.read() if args.text == "-" else args.text
            for msg in rest.post(cfg.token, cid, text, args.file, reply_to=args.reply_to,
                                 max_mb=cfg.max_upload_mb):
                print(msg.get("id", ""))
            return 0
        except (ValueError, rest.RestError) as e:
            print(f"seele {cmd}: {e}", file=sys.stderr)
            return 1
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
        print(f"channels: {', '.join(f'{k}={v}' for k, v in cfg.channels.items()) or 'none named'}")
        print(f"application id: {app_id_from_token(cfg.token)}")
        return 0 if ok else 1

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
