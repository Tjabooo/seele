"""The Discord side: messages become Claude Code prompts, slash commands change settings."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import shlex
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord
from discord import app_commands

from . import claude, usage
from .config import Config, Conversation, State, default_effort, set_default_effort
from .text import code_safe, duration, short_path, split_message, thread_name

log = logging.getLogger("seele")
HOME = str(Path.home())
MODEL_HINTS = ["default", "claude-opus-5-5", "opus", "sonnet", "haiku", "fable"]


@dataclass
class Job:
    channel: discord.abc.Messageable
    prompt: str
    started: float = field(default_factory=time.monotonic)
    run: claude.Run | None = None
    tools: int = 0
    log: list[str] = field(default_factory=list)
    status: discord.Message | None = None
    meta: str = ""
    utilization: float | None = None
    dirty: bool = True


class StopView(discord.ui.View):
    def __init__(self, bot: "Seele", key: int):
        super().__init__(timeout=None)
        self.bot, self.key = bot, key

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹")
    async def stop_button(self, interaction: discord.Interaction, _button):
        if not self.bot.is_owner_id(interaction.user.id):
            return await interaction.response.send_message("Not yours.", ephemeral=True)
        stopped = self.bot.stop(self.key)
        await interaction.response.send_message("Stopping." if stopped else "Nothing is running.",
                                                ephemeral=True)


class RetryView(discord.ui.View):
    """Shown when an account hits its limit: rerun the prompt on another account."""

    def __init__(self, bot: "Seele", key: int, channel, accounts: list[str]):
        super().__init__(timeout=6 * 3600)
        for name in accounts:
            button = discord.ui.Button(label=f"Retry on {name}", style=discord.ButtonStyle.primary)
            button.callback = self._make(bot, key, channel, name)
            self.add_item(button)

    @staticmethod
    def _make(bot, key, channel, name):
        async def callback(interaction: discord.Interaction):
            if not bot.is_owner_id(interaction.user.id):
                return await interaction.response.send_message("Not yours.", ephemeral=True)
            conv = bot.state.get(key)
            conv.account = name
            bot.state.save()
            await interaction.response.send_message(f"Switched this conversation to **{name}**, retrying.")
            bot.submit(key, channel, conv.last_prompt or "Continue.")
        return callback


class SessionSelect(discord.ui.Select):
    def __init__(self, bot: "Seele", key: int, sessions):
        self.bot, self.key = bot, key
        options = [discord.SelectOption(label=(title or sid)[:100], value=sid,
                                        description=f"{time.strftime('%b %d %H:%M', time.localtime(mtime))} · {sid[:8]}")
                   for sid, title, mtime in sessions[:25]]
        super().__init__(placeholder="Pick a session to continue", options=options)

    async def callback(self, interaction: discord.Interaction):
        conv = self.bot.state.get(self.key)
        conv.session_id = self.values[0]
        self.bot.state.save()
        await interaction.response.edit_message(
            content=f"Next message continues session `{self.values[0]}`.", view=None)


class Seele(discord.Client):
    def __init__(self, cfg: Config, state: State, message_content: bool = True):
        intents = discord.Intents.default()
        intents.message_content = message_content
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        self.cfg, self.state = cfg, state
        self.tree = app_commands.CommandTree(self)
        self.jobs: dict[int, Job] = {}
        self.queue: dict[int, list[str]] = {}
        self.slots = asyncio.Semaphore(int(os.environ.get("SEELE_MAX_PARALLEL", "3")))
        self.owners: set[int] = set(cfg.owners)
        self.tasks: set[asyncio.Task] = set()
        register_commands(self)

    # ---------- setup ----------
    async def setup_hook(self):
        for gid in self.cfg.guilds:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        await self.tree.sync()

    async def on_ready(self):
        if not self.owners:
            info = await self.application_info()
            if info.team:
                self.owners = {m.id for m in info.team.members}
            elif info.owner:
                self.owners = {info.owner.id}
        log.info("connected as %s; owners %s", self.user, sorted(self.owners))
        await self.greet()

    async def greet(self):
        """DM each owner once, so the DM channel exists. Retried until it works (needs a mutual
        server or the app added to the owner's account)."""
        flags = self.state.get(0)  # key 0 holds bot-wide flags; last_prompt lists greeted owners
        done = set(flags.last_prompt.split()) if flags.last_prompt else set()
        for uid in self.owners - {int(x) for x in done if x.isdigit()}:
            try:
                user = await self.fetch_user(uid)
                await user.send("**SEELE online.** Message me here to prompt Claude Code on this machine. "
                                "`/usage`, `/effort`, `/cd`, `/new`, `/status` and friends work here too.")
                done.add(str(uid))
            except discord.HTTPException as e:
                log.warning("could not DM owner %s yet: %s", uid, e)
        flags.last_prompt = " ".join(sorted(done))
        self.state.save()

    async def on_interaction(self, interaction: discord.Interaction):
        if self.is_owner_id(interaction.user.id):
            await self.greet()

    async def on_guild_join(self, _guild: discord.Guild):
        await self.greet()

    def is_owner_id(self, uid: int) -> bool:
        return uid in self.owners

    # ---------- messages ----------
    async def on_message(self, msg: discord.Message):
        if msg.author.bot or not self.is_owner_id(msg.author.id):
            return
        channel = msg.channel
        mentioned = self.user in msg.mentions
        content = msg.content
        if self.user:
            content = content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()

        if isinstance(channel, discord.DMChannel):
            key = channel.id
        elif isinstance(channel, discord.Thread):
            parent = self.state.convs.get(str(channel.parent_id))
            if not (self.state.has(channel.id) or mentioned or (parent and parent.listen)):
                return
            if not self.state.has(channel.id):
                self.state.convs[str(channel.id)] = parent.inherit() if parent else Conversation()
            key = channel.id
        else:
            conv = self.state.convs.get(str(channel.id))
            if not (mentioned or (conv and conv.listen)):
                return
            try:  # every new prompt in a channel gets its own thread (= its own Claude session)
                thread = await msg.create_thread(name=thread_name(content or "Claude"),
                                                 auto_archive_duration=1440)
                self.state.convs[str(thread.id)] = conv.inherit() if conv else Conversation()
                channel, key = thread, thread.id
            except discord.HTTPException:
                key = channel.id

        prompt = await self.with_attachments(msg, content)
        if not prompt.strip():
            return
        if key in self.jobs:
            self.queue.setdefault(key, []).append(prompt)
            await msg.add_reaction("📥")
            return
        self.submit(key, channel, prompt)

    async def with_attachments(self, msg: discord.Message, prompt: str) -> str:
        if not msg.attachments:
            return prompt
        folder = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "seele" / "attachments"
        folder.mkdir(parents=True, exist_ok=True)
        lines = []
        for att in msg.attachments:
            path = folder / f"{msg.id}-{Path(att.filename).name}"
            await att.save(path)
            lines.append(f"- {path} ({att.content_type or 'file'}, {att.size // 1024} KB)")
        return (prompt or "See the attached files.") + "\n\nAttached files (saved on this machine):\n" + "\n".join(lines)

    def submit(self, key: int, channel, prompt: str):
        self.jobs[key] = Job(channel, prompt)  # placeholder: marks the chat busy right away
        task = asyncio.create_task(self.run_job(key, channel, prompt))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def stop(self, key: int) -> bool:
        self.queue.pop(key, None)
        job = self.jobs.get(key)
        if job and job.run:
            job.run.stop()
            return True
        return False

    # ---------- running Claude ----------
    def settings_for(self, conv: Conversation):
        cwd = Path(conv.cwd or self.cfg.default_cwd)
        account = self.cfg.account_for(cwd, conv.account or None)
        model = conv.model or self.cfg.model
        effort = conv.effort or account.effort or self.cfg.effort
        mode = conv.mode or self.cfg.permission_mode
        return cwd, account, model, effort, mode

    def child_env(self, account) -> dict[str, str]:
        env = {**os.environ, **self.cfg.env}
        if account.config_dir.resolve() == (Path.home() / ".claude").resolve():
            env.pop("CLAUDE_CONFIG_DIR", None)  # default dir keeps ~/.claude.json
        else:
            env["CLAUDE_CONFIG_DIR"] = str(account.config_dir)
        return env

    async def guard_active(self) -> bool:
        if not self.cfg.guard_process:
            return False
        proc = await asyncio.create_subprocess_exec("pgrep", "-x", self.cfg.guard_process,
                                                    stdout=asyncio.subprocess.DEVNULL)
        return await proc.wait() == 0

    async def run_job(self, key: int, channel, prompt: str, retried: bool = False):
        conv = self.state.get(key)
        conv.last_prompt = prompt
        cwd, account, model, effort, mode = self.settings_for(conv)
        if not cwd.is_dir():
            self.jobs.pop(key, None)
            await channel.send(f"⚠️ Working folder `{cwd}` doesn't exist. Use `/cd`.")
            return
        system = self.cfg.system_prompt
        guard = await self.guard_active()
        if guard and self.cfg.guard_note:
            system += "\n\n" + self.cfg.guard_note
        argv = claude.build_argv(self.cfg.claude_bin, session_id=conv.session_id or None, model=model or None,
                                 effort=effort or None, mode=mode, system_prompt=system)
        job = Job(channel, prompt)
        job.meta = " · ".join(filter(None, [
            short_path(str(cwd), HOME), account.name, model or "default model",
            f"effort {effort or default_effort(self.cfg.settings_file) or 'default'}", mode,
            "🎮 guard on" if guard else ""]))
        self.jobs[key] = job
        ticker = None
        try:
            job.status = await channel.send(self.render(job, "⏳ **Queued**" if self.slots.locked() else None),
                                            view=StopView(self, key))
            async with self.slots:
                job.run = claude.Run(argv, prompt, cwd, self.child_env(account))
                ticker = asyncio.create_task(self.tick(job))
                result = None
                async with channel.typing():
                    async for ev in job.run.events():
                        if ev.kind == "init" and ev.session_id:
                            conv.session_id = ev.session_id
                            self.state.save()
                        elif ev.kind == "tool":
                            job.tools += 1
                            job.log.append("▸ " + ev.text)
                        elif ev.kind == "text":
                            job.log.append("· " + " ".join(ev.text.split())[:140])
                        elif ev.kind == "rate":
                            job.utilization = ev.utilization
                        elif ev.kind == "result":
                            result = ev
                        job.dirty = True
            ticker.cancel()
            if result and result.session_id:
                conv.session_id = result.session_id
            self.state.save()
            if result and result.is_error and not retried and "No conversation found" in result.text:
                conv.session_id = ""
                self.state.save()
                await job.status.edit(content=self.render(job, "↻ **Old session not found, starting fresh**"),
                                      view=None)
                return await self.run_job(key, channel, prompt, retried=True)
            await self.finish(key, job, result, account.name)
        except Exception as e:  # never leave a conversation stuck
            log.exception("job failed")
            await channel.send(f"⚠️ Bot error: `{e}`")
        finally:
            if ticker:
                ticker.cancel()
            self.jobs.pop(key, None)
            pending = self.queue.pop(key, None)
            if pending:
                self.submit(key, channel, "\n\n".join(pending))

    async def tick(self, job: Job):
        last = 0.0
        while True:
            await asyncio.sleep(2)
            if job.dirty or time.monotonic() - last > 15:
                job.dirty = False
                last = time.monotonic()
                try:
                    await job.status.edit(content=self.render(job))
                except discord.HTTPException:
                    pass

    def render(self, job: Job, head: str | None = None) -> str:
        elapsed = duration(time.monotonic() - job.started)
        head = head or f"⏳ **Working** · {elapsed} · {job.tools} tool{'s' * (job.tools != 1)}"
        if job.utilization is not None:
            head += f" · 5 h {job.utilization * 100:.0f}%"
        body = ""
        if job.log:
            lines = [code_safe(line)[:150] for line in job.log[-10:]]
            body = "\n```\n" + "\n".join(lines) + "\n```"
        return f"{head}\n-# {job.meta}{body}"[:1990]

    async def finish(self, key: int, job: Job, result: claude.Event | None, account_name: str):
        conv = self.state.get(key)
        elapsed = duration(time.monotonic() - job.started)
        stats = f"{elapsed} · {job.tools} tool{'s' * (job.tools != 1)}"
        if job.utilization is not None:
            stats += f" · 5 h {job.utilization * 100:.0f}%"
        if conv.session_id:
            stats += f" · `{conv.session_id[:8]}`"
        if result is None:
            result = claude.Event("result", text="No result.", is_error=True)
        if result.data.get("subtype") == "stopped":
            head = f"⏹ **Stopped** · {stats}"
        elif result.is_error:
            head = f"⚠️ **Failed** · {stats}"
        else:
            head = f"✅ **Done** · {stats}"
        try:
            await job.status.edit(content=f"{head}\n-# {job.meta}", view=None)
        except discord.HTTPException:
            pass
        if result.data.get("subtype") == "stopped":
            return
        view = None
        if result.is_error and claude.looks_like_limit(result.text):
            others = [a.name for a in self.cfg.accounts if a.name != account_name]
            if others:
                view = RetryView(self, key, job.channel, others)
        await self.send_long(job.channel, result.text, view=view)

    async def send_long(self, channel, text: str, view=None):
        pieces = split_message(text)
        extra = {"view": view} if view else {}
        if len(pieces) > 3:
            await channel.send(pieces[0] + "\n-# …full reply attached",
                               file=discord.File(io.BytesIO(text.encode()), "reply.md"), **extra)
            return
        for i, piece in enumerate(pieces):
            await channel.send(piece, **(extra if i == len(pieces) - 1 else {}))


# ---------- slash commands ----------
def register_commands(bot: Seele):
    tree = bot.tree
    installs = app_commands.allowed_installs(guilds=True, users=True)
    contexts = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)

    def command(name: str, description: str):
        def wrap(fn):
            return tree.command(name=name, description=description)(installs(contexts(fn)))
        return wrap

    async def guard(interaction: discord.Interaction) -> bool:
        if bot.is_owner_id(interaction.user.id):
            return True
        await interaction.response.send_message("This bot only takes orders from its owner.", ephemeral=True)
        return False

    def conv_of(interaction: discord.Interaction) -> Conversation:
        return bot.state.get(interaction.channel_id)

    @command("new", "Start a fresh Claude session here (keeps folder, model and effort)")
    async def new(interaction: discord.Interaction):
        if not await guard(interaction):
            return
        conv = conv_of(interaction)
        conv.session_id = ""
        bot.state.save()
        cwd, *_ = bot.settings_for(conv)
        await interaction.response.send_message(f"🆕 Next message starts a fresh session in `{short_path(str(cwd), HOME)}`.")

    @command("cd", "Change the working folder (starts a fresh session)")
    @app_commands.describe(path="Folder, e.g. a project name or ~/some/path")
    async def cd(interaction: discord.Interaction, path: str):
        if not await guard(interaction):
            return
        target = Path(os.path.expanduser(path))
        if not target.is_absolute():
            target = bot.cfg.projects_root / target
        if not target.is_dir():
            return await interaction.response.send_message(f"`{path}` is not a folder.", ephemeral=True)
        conv = conv_of(interaction)
        conv.cwd, conv.session_id = str(target.resolve()), ""
        bot.state.save()
        acc = bot.cfg.account_for(target, conv.account or None)
        await interaction.response.send_message(
            f"📁 Working in `{short_path(conv.cwd, HOME)}` (account **{acc.name}**). Fresh session next message.")

    @cd.autocomplete("path")
    async def cd_complete(_interaction, current: str):
        return [app_commands.Choice(name=n[:100], value=v[:100]) for n, v in folder_choices(bot.cfg, current)]

    @command("effort", "Set the effort level for this conversation, or Claude Code's default")
    @app_commands.describe(level="Effort level", everywhere="Also save it as Claude Code's default (like /effort)")
    @app_commands.choices(level=[app_commands.Choice(name=x, value=x) for x in (*claude.EFFORTS, "default")])
    async def effort(interaction: discord.Interaction, level: app_commands.Choice[str], everywhere: bool = False):
        if not await guard(interaction):
            return
        conv = conv_of(interaction)
        if everywhere and level.value != "default":
            models = set_default_effort(bot.cfg.settings_file, level.value)
            conv.effort = ""
            bot.state.save()
            return await interaction.response.send_message(
                f"⚙️ Claude Code's default effort is now **{level.value}** ({', '.join(models)}), here and in the terminal.")
        conv.effort = "" if level.value == "default" else level.value
        bot.state.save()
        shown = conv.effort or f"default ({default_effort(bot.cfg.settings_file) or 'CLI default'})"
        await interaction.response.send_message(f"⚙️ Effort here: **{shown}**.")

    @command("model", "Set the model for this conversation")
    @app_commands.describe(name="claude-opus-5-5, opus, sonnet, haiku, fable, a full model id, or default")
    async def model(interaction: discord.Interaction, name: str):
        if not await guard(interaction):
            return
        conv = conv_of(interaction)
        conv.model = "" if name == "default" else name.strip()
        bot.state.save()
        await interaction.response.send_message(f"🧠 Model here: **{conv.model or 'default'}**.")

    @model.autocomplete("name")
    async def model_complete(_interaction, current: str):
        return [app_commands.Choice(name=m, value=m) for m in MODEL_HINTS if current.lower() in m][:25]

    @command("mode", "Set the permission mode for this conversation")
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in (*claude.MODES, "default")])
    async def mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not await guard(interaction):
            return
        conv = conv_of(interaction)
        conv.mode = "" if mode.value == "default" else mode.value
        bot.state.save()
        warn = " ⚠️ No permission checks at all." if conv.mode == "bypassPermissions" else ""
        await interaction.response.send_message(f"🔐 Mode here: **{conv.mode or bot.cfg.permission_mode}**.{warn}")

    @command("account", "Pick which Claude account runs this conversation")
    @app_commands.describe(name="Account name, or auto (by folder)")
    async def account(interaction: discord.Interaction, name: str):
        if not await guard(interaction):
            return
        names = [a.name for a in bot.cfg.accounts]
        if name != "auto" and name not in names:
            return await interaction.response.send_message(f"Unknown account. Pick one of: {', '.join(names)}, auto.",
                                                           ephemeral=True)
        conv = conv_of(interaction)
        conv.account = "" if name == "auto" else name
        bot.state.save()
        cwd, acc, *_ = bot.settings_for(conv)
        await interaction.response.send_message(f"👤 Account here: **{acc.name}**{' (auto)' if not conv.account else ''}.")

    @account.autocomplete("name")
    async def account_complete(_interaction, current: str):
        return [app_commands.Choice(name=n, value=n) for n in ["auto", *[a.name for a in bot.cfg.accounts]]
                if current.lower() in n.lower()]

    @command("usage", "Plan usage for every Claude account (like /usage)")
    async def usage_cmd(interaction: discord.Interaction):
        if not await guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        embed = discord.Embed(title="Claude usage", color=0xE8590C)
        results = await asyncio.gather(*[
            usage.for_account(bot.cfg.claude_bin, a.config_dir, bot.child_env(a)) for a in bot.cfg.accounts])
        for acc, text in zip(bot.cfg.accounts, results):
            embed.add_field(name=acc.name, value=text[:1024], inline=False)
        running = len(bot.jobs)
        embed.set_footer(text=f"{running} prompt{'s' * (running != 1)} running")
        await interaction.followup.send(embed=embed)

    @command("cost", "What your Claude Code (and Codex) chats would cost at API prices")
    @app_commands.describe(days="Only the last N days")
    async def cost(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 7):
        if not await guard(interaction):
            return
        cmd = bot.cfg.cost_command or shutil.which("cost") or ""
        if not cmd:
            return await interaction.response.send_message("No cost command configured.", ephemeral=True)
        await interaction.response.defer(thinking=True)
        proc = await asyncio.create_subprocess_exec(*shlex.split(cmd), "-d", str(days),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    env={**os.environ, "NO_COLOR": "1"})
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 180)
        except TimeoutError:
            proc.kill()
            return await interaction.followup.send("cost timed out.")
        text = out.decode(errors="replace").strip()
        if len(text) < 1900:
            await interaction.followup.send(f"```\n{code_safe(text)}\n```")
        else:
            await interaction.followup.send(f"Cost, last {days} days:", file=discord.File(io.BytesIO(text.encode()), "cost.txt"))

    @command("status", "Show this conversation's settings and what's running")
    async def status(interaction: discord.Interaction):
        if not await guard(interaction):
            return
        conv = conv_of(interaction)
        cwd, acc, model_, effort_, mode_ = bot.settings_for(conv)
        job = bot.jobs.get(interaction.channel_id)
        saved = default_effort(bot.cfg.settings_file) or "CLI default"
        lines = [
            f"**Folder** `{short_path(str(cwd), HOME)}`",
            f"**Account** {acc.name}{' (auto)' if not conv.account else ''}",
            f"**Model** {model_ or 'default'}",
            f"**Effort** {effort_ or 'default (' + saved + ')'}",
            f"**Mode** {mode_}",
        ]
        if conv.session_id:
            prefix = "" if acc.config_dir.resolve() == (Path.home() / ".claude").resolve() \
                else f"CLAUDE_CONFIG_DIR={short_path(str(acc.config_dir), HOME)} "
            lines.append(f"**Session** `{conv.session_id}` · terminal: `cd {short_path(str(cwd), HOME)} && "
                         f"{prefix}claude --resume {conv.session_id}`")
        else:
            lines.append("**Session** fresh on next message")
        if job:
            queued = len(bot.queue.get(interaction.channel_id, []))
            lines.append(f"**Running** {duration(time.monotonic() - job.started)}, {job.tools} tools"
                         + (f", {queued} queued" if queued else ""))
        lines.append(f"**Busy here** {len(bot.jobs)} prompt(s) running across all chats")
        if await bot.guard_active():
            lines.append(f"🎮 `{bot.cfg.guard_process}` is running: Claude is told to keep it light")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @command("stop", "Stop the running prompt here and drop queued messages")
    async def stop(interaction: discord.Interaction):
        if not await guard(interaction):
            return
        stopped = bot.stop(interaction.channel_id)
        await interaction.response.send_message("⏹ Stopping." if stopped else "Nothing is running here.")

    @command("sessions", "Continue one of the recent Claude sessions in this folder (also terminal ones)")
    async def sessions(interaction: discord.Interaction):
        if not await guard(interaction):
            return
        conv = conv_of(interaction)
        cwd, acc, *_ = bot.settings_for(conv)
        found = await asyncio.to_thread(claude.recent_sessions, acc.config_dir, cwd, 25)
        if not found:
            return await interaction.response.send_message(f"No sessions in `{short_path(str(cwd), HOME)}` yet.",
                                                           ephemeral=True)
        view = discord.ui.View(timeout=600)
        view.add_item(SessionSelect(bot, interaction.channel_id, found))
        await interaction.response.send_message(f"Sessions in `{short_path(str(cwd), HOME)}`:", view=view,
                                                ephemeral=True)

    @command("get", "Upload a file from this machine")
    @app_commands.describe(path="Absolute path or ~/path")
    async def get(interaction: discord.Interaction, path: str):
        if not await guard(interaction):
            return
        target = Path(os.path.expanduser(path))
        if not target.is_absolute():
            target = Path(conv_of(interaction).cwd or bot.cfg.default_cwd) / target
        if not target.is_file():
            return await interaction.response.send_message(f"`{path}` is not a file.", ephemeral=True)
        if target.stat().st_size > bot.cfg.max_upload_mb * 1024 * 1024:
            return await interaction.response.send_message(f"Bigger than {bot.cfg.max_upload_mb} MB.", ephemeral=True)
        await interaction.response.send_message(file=discord.File(target))

    @command("listen", "In a server channel: answer every message here, not only mentions")
    async def listen(interaction: discord.Interaction, on: bool = True):
        if not await guard(interaction):
            return
        if interaction.guild is None:
            return await interaction.response.send_message("DMs always listen.", ephemeral=True)
        conv = conv_of(interaction)
        conv.listen = on
        bot.state.save()
        await interaction.response.send_message(
            "👂 Every message here now becomes a prompt in its own thread." if on else "Only mentions from now on.")

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("command failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        try:
            await send(f"⚠️ {error}", ephemeral=True)
        except discord.HTTPException:
            pass


def folder_choices(cfg: Config, current: str, limit: int = 25) -> list[tuple[str, str]]:
    """Autocomplete for /cd: project names, or path completion for anything with a slash or ~."""
    current = current.strip()
    if "/" in current or current.startswith("~"):
        typed = Path(os.path.expanduser(current))
        base, stem = (typed, "") if current.endswith("/") else (typed.parent, typed.name)
        try:
            dirs = sorted(p for p in base.iterdir() if p.is_dir() and p.name.lower().startswith(stem.lower())
                          and (stem.startswith(".") or not p.name.startswith(".")))
        except OSError:
            return []
        return [(short_path(str(p), HOME), str(p)) for p in dirs[:limit]]
    out = [("~ (home)", HOME)] if "home".startswith(current.lower()) or not current else []
    try:
        projects = sorted(p for p in cfg.projects_root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        projects = []
    out += [(p.name, str(p)) for p in projects if current.lower() in p.name.lower()]
    return out[:limit]
