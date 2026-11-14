"""The Discord side: messages become Claude Code prompts, slash commands change settings."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shlex
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

from . import agents, claude, config, usage
from .config import (SPEAKER_NOTE, Config, Conversation, State, default_effort, set_default_effort,
                     tag_prompt)
from .text import code_safe, duration, short_path, split_message, thread_name

log = logging.getLogger("seele")
HOME = str(Path.home())
MODEL_HINTS = ["default", "claude-opus-5-5", "opus", "sonnet", "haiku", "fable"]
AUTORESUME_PROMPT = ("The usage limit has reset (SEELE auto-resume). Continue: resume every agent the "
                     "limit stopped (SendMessage), then carry on.")
RESUME_DELAY = 300  # seconds after the limit resets before the auto-resume prompt goes in
POLL_EVERY = 3.0  # seconds between looks at the requests spool and scheduled resumes
OFF = ("", "off", "none", "no", "0")


class RequestError(Exception):
    """A `seele conv` / `seele prompt` request that can't be applied (the reason goes back to the CLI)."""


@dataclass
class Pending:
    """A prompt waiting for the conversation's current job to end."""
    prompt: str
    human: bool = True
    new_session: bool = False


@dataclass
class Job:
    channel: discord.abc.Messageable
    prompt: str
    key: int = 0
    started: float = field(default_factory=time.monotonic)
    run: claude.Run | None = None
    tools: int = 0
    log: list[str] = field(default_factory=list)
    status: discord.Message | None = None
    view: discord.ui.View | None = None
    meta: str = ""
    utilization: float | None = None
    dirty: bool = True
    results: int = 0  # replies posted so far (one per turn)
    last_result: claude.Event | None = None
    limit_reset: float | None = None  # when a rejected rate limit lifts (epoch seconds)
    tracker: agents.Tracker | None = None
    resume_failed: bool = False


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
            bot.enqueue(key, channel, conv.last_prompt or "Continue.", human=True)
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


def when_text(at: float, now: float | None = None) -> str:
    """A Discord timestamp: every reader sees it in their own local time."""
    now = time.time() if now is None else now
    style = "f" if at - now > 20 * 3600 else "t"
    return f"<t:{int(at)}:{style}> (<t:{int(at)}:R>)"


class Seele(discord.Client):
    def __init__(self, cfg: Config, state: State, message_content: bool = True):
        intents = discord.Intents.default()
        intents.message_content = message_content
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        self.cfg, self.state = cfg, state
        self.tree = app_commands.CommandTree(self)
        self.jobs: dict[int, Job] = {}
        self.queue: dict[int, list[Pending]] = {}
        self.slots = asyncio.Semaphore(int(os.environ.get("SEELE_MAX_PARALLEL", "3")))
        self.owners: set[int] = set(cfg.owners)
        self.tasks: set[asyncio.Task] = set()
        self.board = agents.Board(state, self.channel_by_id)
        self.requests_dir = config.REQUESTS_DIR
        register_commands(self)

    # ---------- setup ----------
    async def setup_hook(self):
        for gid in self.cfg.guilds:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        await self.tree.sync()
        self.spawn(self.poll_forever())

    async def on_ready(self):
        if not self.owners:
            info = await self.application_info()
            if info.team:
                self.owners = {m.id for m in info.team.members}
            elif info.owner:
                self.owners = {info.owner.id}
        log.info("connected as %s; owners %s", self.user, sorted(self.owners))
        await self.greet()
        try:
            await self.board.orphans()
        except Exception:
            log.exception("could not tidy up old agent cards")

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

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def channel_by_id(self, cid: int):
        channel = self.get_channel(cid)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(cid)
        except (discord.HTTPException, discord.InvalidData):
            return None

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
        name = self.cfg.name_for(msg.author.id, msg.author.display_name)
        where = await self.deliver(key, channel, tag_prompt(name, prompt))
        if where != "started":
            await msg.add_reaction("📨" if where == "live" else "📥")

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

    async def deliver(self, key: int, channel, prompt: str, human: bool = True) -> str:
        """A message for a conversation: start a job, write it into the running Claude process
        ("live"), or queue it for when the job ends. Returns which one happened."""
        job = self.jobs.get(key)
        if job is None:
            self.submit(key, channel, prompt, human=human)
            return "started"
        if job.run and await job.run.send(prompt):
            job.log.append("📨 " + " ".join(prompt.split())[:140])
            job.dirty = True
            return "live"
        self.queue.setdefault(key, []).append(Pending(prompt, human))
        return "queued"

    def enqueue(self, key: int, channel, prompt: str, *, human: bool = False, new_session: bool = False) -> str:
        """Run a prompt once the conversation's current job (if any) has ended."""
        if key in self.jobs:
            self.queue.setdefault(key, []).append(Pending(prompt, human, new_session))
            return "queued"
        self.submit(key, channel, prompt, human=human, new_session=new_session)
        return "started"

    def submit(self, key: int, channel, prompt: str, *, human: bool = True, new_session: bool = False):
        self.jobs[key] = Job(channel, prompt, key=key)  # placeholder: marks the chat busy right away
        self.spawn(self.run_job(key, channel, prompt, human=human, new_session=new_session))

    def stop(self, key: int) -> bool:
        self.queue.pop(key, None)
        conv = self.state.convs.get(str(key))
        if conv and conv.resume_at:
            conv.resume_at = 0
            self.state.save()
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
        return config.account_env(self.cfg, account)

    async def guard_active(self) -> bool:
        if not self.cfg.guard_process:
            return False
        proc = await asyncio.create_subprocess_exec("pgrep", "-x", self.cfg.guard_process,
                                                    stdout=asyncio.subprocess.DEVNULL)
        return await proc.wait() == 0

    async def run_job(self, key: int, channel, prompt: str, *, human: bool = True, new_session: bool = False):
        try:
            conv = self.state.get(key)
            if new_session:
                conv.session_id = ""
                self.state.save()
            if human and conv.resume_at:
                conv.resume_at = 0
                self.state.save()
                await channel.send("-# ⏸ Auto-resume cancelled: a new prompt started first.")
            retried = False
            while await self.attempt(key, channel, prompt, retried):
                retried = True
        except Exception as e:  # never leave a conversation stuck
            log.exception("job failed")
            try:
                await channel.send(f"⚠️ Bot error: `{e}`")
            except discord.HTTPException:
                pass
        finally:
            self.jobs.pop(key, None)
            pending = self.queue.pop(key, None)
            if pending:
                self.submit(key, channel, "\n\n".join(p.prompt for p in pending),
                            human=any(p.human for p in pending), new_session=any(p.new_session for p in pending))

    async def attempt(self, key: int, channel, prompt: str, retried: bool) -> bool:
        """One Claude Code process for this prompt. True = its old session is gone, run again fresh."""
        conv = self.state.get(key)
        conv.last_prompt = prompt
        cwd, account, model, effort, mode = self.settings_for(conv)
        if not cwd.is_dir():
            await channel.send(f"⚠️ Working folder `{cwd}` doesn't exist. Use `/cd`.")
            return False
        system = self.cfg.system_prompt + "\n\n" + SPEAKER_NOTE
        if self.cfg.people:
            system += " Known people: " + ", ".join(sorted(set(self.cfg.people.values()))) + "."
        guard = await self.guard_active()
        if guard and self.cfg.guard_note:
            system += "\n\n" + self.cfg.guard_note
        live = self.cfg.live_input
        argv = claude.build_argv(self.cfg.claude_bin, session_id=conv.session_id or None, model=model or None,
                                 effort=effort or None, mode=mode, system_prompt=system, live=live)
        job = Job(channel, prompt, key=key)
        job.meta = " · ".join(filter(None, [
            short_path(str(cwd), HOME), account.name, model or "default model",
            f"effort {effort or default_effort(self.cfg.settings_file) or 'default'}", mode,
            "🎮 guard on" if guard else ""]))
        job.tracker = agents.Tracker(self.board, key, conv, getattr(channel, "jump_url", "") or "")
        self.jobs[key] = job
        ticker = None
        try:
            job.view = StopView(self, key)
            job.status = await channel.send(self.render(job, "⏳ **Queued**" if self.slots.locked() else None),
                                            view=job.view)
            async with self.slots:
                job.run = claude.Run(argv, prompt, cwd, self.child_env(account), live=live)
                ticker = asyncio.create_task(self.tick(job))
                async for ev in job.run.events():
                    try:
                        await self.on_event(key, job, ev, account, retried)
                    except Exception:  # a Discord hiccup must not orphan the Claude process
                        log.exception("could not handle a %s event", ev.kind)
            ticker.cancel()
            await job.tracker.close()
            self.state.save()
            if job.resume_failed and not job.results:
                conv.session_id = ""
                self.state.save()
                await job.status.edit(content=self.render(job, "↻ **Old session not found, starting fresh**"),
                                      view=None)
                return True
            await self.finish(key, job)
            return False
        finally:
            if ticker:
                ticker.cancel()
            if job.view:
                job.view.stop()
            if job.run and job.run.proc and job.run.proc.returncode is None:
                job.run.stop()

    async def on_event(self, key: int, job: Job, ev: claude.Event, account, retried: bool):
        conv = self.state.get(key)
        if ev.agent or ev.kind in claude.TASK_EVENTS:  # subagents: their cards, not the main status
            await job.tracker.handle(ev)
        elif ev.kind == "init":
            if ev.session_id and ev.session_id != conv.session_id:
                conv.session_id = ev.session_id
                self.state.save()
        elif ev.kind == "tool":
            job.tools += 1
            job.log.append("▸ " + ev.text)
        elif ev.kind == "text":
            job.log.append("· " + " ".join(ev.text.split())[:140])
        elif ev.kind == "rate":
            if ev.utilization is not None:
                job.utilization = ev.utilization
            job.limit_reset = claude.limit_reset(ev.data) or job.limit_reset
        elif ev.kind == "result":
            if ev.session_id:
                conv.session_id = ev.session_id
            if not retried and not job.results and ev.is_error and "No conversation found" in ev.text:
                job.resume_failed = True
                return
            job.last_result = ev
            await self.on_result(key, job, ev, account)
        if len(job.log) > 200:
            del job.log[:-50]
        job.dirty = True

    async def on_result(self, key: int, job: Job, result: claude.Event, account):
        """Each turn's answer goes out as soon as it arrives; the live status then moves below it
        while Claude carries on (more turns, background agents)."""
        if result.data.get("subtype") == "stopped":
            return
        if job.results and not result.is_error and not result.text.strip():
            return  # an empty follow-up turn (e.g. after a background task ended): nothing to say
        job.results += 1
        limit = result.is_error and claude.looks_like_limit(result.text)
        view = None
        if limit:
            others = [a.name for a in self.cfg.accounts if a.name != account.name]
            if others:
                view = RetryView(self, key, job.channel, others)
        await self.send_long(job.channel, result.text, view=view)
        if limit and self.state.get(key).autoresume:
            await self.schedule_resume(key, job, result, account)
        run = job.run
        if run and run.proc and run.proc.returncode is None and (run.input_open or run.tasks):
            await self.move_status(job)

    async def move_status(self, job: Job):
        """Post a fresh status below the latest reply and drop the old one."""
        old, old_view = job.status, job.view
        job.view = StopView(self, job.key)
        try:
            job.status = await job.channel.send(self.render(job), view=job.view)
        except discord.HTTPException:
            job.view.stop()
            job.status, job.view = old, old_view
            return
        if old_view:
            old_view.stop()
        try:
            await old.delete()
        except (discord.HTTPException, AttributeError):
            pass

    async def tick(self, job: Job):
        last = typed = 0.0
        while True:
            await asyncio.sleep(2)
            now = time.monotonic()
            active = bool(job.run and job.run.turn_active)
            if active and now - typed > 8:  # "typing…" only while a turn runs, not while waiting on agents
                typed = now
                try:
                    await job.channel.typing()
                except Exception:
                    pass
            if job.tracker:
                try:
                    await job.tracker.flush()
                except Exception:
                    log.exception("agent cards")
            if job.dirty or now - last > (15 if active else 60):
                job.dirty = False
                last = now
                try:
                    await job.status.edit(content=self.render(job))
                except discord.HTTPException:
                    pass

    def render(self, job: Job, head: str | None = None) -> str:
        elapsed = duration(time.monotonic() - job.started)
        run = job.run
        if head is None:
            if run and job.results and not run.turn_active and run.tasks:
                n = len(run.tasks)
                head = f"💤 **Waiting** · {elapsed} · {n} background task{'s' * (n != 1)}"
            else:
                head = f"⏳ **Working** · {elapsed} · {job.tools} tool{'s' * (job.tools != 1)}"
            if job.results:
                head += f" · {job.results} repl{'ies' if job.results != 1 else 'y'}"
        if job.utilization is not None:
            head += f" · 5 h {job.utilization * 100:.0f}%"
        tail = self.agent_lines(job)
        body = ""
        if job.log:
            lines = [code_safe(line)[:150] for line in job.log[-10:]]
            room = 1900 - len(head) - len(job.meta) - len(tail) - 20
            while lines and sum(len(x) + 1 for x in lines) > room:
                lines.pop(0)
            if lines:
                body = "\n```\n" + "\n".join(lines) + "\n```"
        return f"{head}\n-# {job.meta}{body}{tail}"[:1990]

    def agent_lines(self, job: Job) -> str:
        running = job.tracker.running() if job.tracker else []
        if not running:
            return ""
        cid = job.tracker.channel_id()
        if cid:
            return f"\n🤖 {len(running)} agent{'s' * (len(running) != 1)} running · cards in <#{cid}>"
        now = time.time()
        lines = [agents.compact_line(card, now) for card in running[:6]]
        if len(running) > 6:
            lines.append(f"…and {len(running) - 6} more")
        return "\n" + "\n".join(lines)

    async def finish(self, key: int, job: Job):
        conv = self.state.get(key)
        elapsed = duration(time.monotonic() - job.started)
        stats = f"{elapsed} · {job.tools} tool{'s' * (job.tools != 1)}"
        if job.results > 1:
            stats += f" · {job.results} replies"
        if job.utilization is not None:
            stats += f" · 5 h {job.utilization * 100:.0f}%"
        if conv.session_id:
            stats += f" · `{conv.session_id[:8]}`"
        result = job.last_result or claude.Event("result", text="No result.", is_error=True)
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

    async def send_long(self, channel, text: str, view=None):
        pieces = split_message(text)
        extra = {"view": view} if view else {}
        if len(pieces) > 3:
            await channel.send(pieces[0] + "\n-# …full reply attached",
                               file=discord.File(io.BytesIO(text.encode()), "reply.md"), **extra)
            return
        for i, piece in enumerate(pieces):
            await channel.send(piece, **(extra if i == len(pieces) - 1 else {}))

    # ---------- auto-resume after a usage limit ----------
    async def schedule_resume(self, key: int, job: Job, result: claude.Event, account):
        conv = self.state.get(key)
        reset = job.limit_reset or claude.reset_from_text(result.text) or await self.reset_from_usage(account)
        guessed = reset is None
        now = time.time()
        at = max(reset or now + 3600, now) + RESUME_DELAY
        if conv.resume_at and abs(conv.resume_at - at) < 120:
            return  # already scheduled and announced
        conv.resume_at = at
        self.state.save()
        note = " (couldn't read the reset time, so trying in an hour)" if guessed else ""
        await job.channel.send(f"⏸ Usage limit hit; resuming automatically at {when_text(at, now)}{note}.")

    async def reset_from_usage(self, account) -> float | None:
        token = usage.read_token(account.config_dir)
        if not token:
            return None
        try:
            data = await asyncio.to_thread(usage.fetch, token)
        except Exception:
            return None
        resets = []
        for _label, pct, iso in usage.rows(data):
            if pct >= 99.5 and iso:
                try:
                    resets.append(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    pass
        return max(resets) if resets else None

    async def check_resumes(self):
        now = time.time()
        for key_text, conv in list(self.state.convs.items()):
            if not conv.resume_at or conv.resume_at > now:
                continue
            conv.resume_at = 0
            self.state.save()
            key = int(key_text)
            channel = await self.channel_by_id(key)
            if channel is None:
                log.warning("auto-resume: channel %s is gone", key)
                continue
            try:
                await channel.send("▶️ The usage limit has reset: resuming.")
            except discord.HTTPException:
                pass
            self.enqueue(key, channel, AUTORESUME_PROMPT, human=False)

    # ---------- requests from the `seele` CLI (conv / prompt) ----------
    async def poll_forever(self):
        while True:
            await asyncio.sleep(POLL_EVERY)
            try:
                await self.poll_requests()
                await self.check_resumes()
            except Exception:
                log.exception("request poll failed")

    async def poll_requests(self):
        folder = self.requests_dir
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.json")):
            try:
                if path.stat().st_uid != os.getuid():  # only requests written by this user
                    log.warning("ignoring %s: not ours", path.name)
                    path.rename(path.with_suffix(".rejected"))
                    continue
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                log.warning("bad request %s: %s", path.name, e)
                try:
                    path.rename(path.with_suffix(".bad"))
                except OSError:
                    pass
                continue
            path.unlink(missing_ok=True)
            try:
                ok, message = True, await self.apply_request(data)
            except RequestError as e:
                ok, message = False, str(e)
            except Exception as e:
                log.exception("request %s failed", path.name)
                ok, message = False, f"bot error: {e}"
            log.info("request %s (%s): %s", path.name, data.get("kind"), message)
            done = folder / (path.stem + ".done")
            tmp = folder / ("." + path.stem + ".done.tmp")
            tmp.write_text(json.dumps({"ok": ok, "message": message}))
            tmp.replace(done)
        cutoff = time.time() - 3600
        for old in folder.glob("*.done"):  # answers nobody waited for
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    async def apply_request(self, data: dict) -> str:
        kind = data.get("kind")
        try:
            key = self.cfg.channel_id(data.get("id", ""))
        except ValueError as e:
            raise RequestError(str(e)) from None
        if kind == "conv":
            return await self.apply_conv(key, data.get("set") or {})
        if kind == "prompt":
            return await self.apply_prompt(key, data)
        raise RequestError(f"unknown request kind {kind!r}")

    async def ensure_conv(self, key: int) -> Conversation:
        """A conversation's settings; a thread seen for the first time inherits its channel's."""
        if not self.state.has(key):
            channel = await self.channel_by_id(key)
            parent = getattr(channel, "parent_id", None)
            if parent and self.state.has(parent):
                self.state.convs[str(key)] = self.state.get(parent).inherit()
        return self.state.get(key)

    def _setting(self, name: str, value) -> tuple[str, object]:
        """Validate one `seele conv` setting: (field, new value)."""
        text = str(value).strip()
        if name == "cwd":
            target = Path(os.path.expanduser(text))
            if not target.is_absolute():
                target = self.cfg.projects_root / target
            if not target.is_dir():
                raise RequestError(f"{text} is not a folder")
            return "cwd", str(target.resolve())
        if name == "account":
            names = [a.name for a in self.cfg.accounts]
            if text != "auto" and text not in names:
                raise RequestError(f"unknown account {text!r}; pick one of {', '.join(names)}, auto")
            return "account", "" if text == "auto" else text
        if name == "effort":
            if text not in (*claude.EFFORTS, "default"):
                raise RequestError(f"effort must be one of {', '.join(claude.EFFORTS)}, default")
            return "effort", "" if text == "default" else text
        if name == "model":
            return "model", "" if text == "default" else text
        if name == "mode":
            if text not in (*claude.MODES, "default"):
                raise RequestError(f"mode must be one of {', '.join(claude.MODES)}, default")
            return "mode", "" if text == "default" else text
        if name == "session":
            return "session_id", "" if text.lower() in (*OFF, "new") else text
        if name == "agents_channel":
            if text.lower() in OFF:
                return "agents_channel", ""
            try:
                return "agents_channel", str(self.cfg.channel_id(text))
            except ValueError as e:
                raise RequestError(str(e)) from None
        if name in ("autoresume", "listen"):
            if isinstance(value, bool):
                return name, value
            if text.lower() in ("on", "true", "yes", "1"):
                return name, True
            if text.lower() in OFF + ("false",):
                return name, False
            raise RequestError(f"{name} must be on or off")
        raise RequestError(f"unknown setting {name!r}")

    async def apply_conv(self, key: int, changes: dict) -> str:
        new = dict(self._setting(k, v) for k, v in changes.items())  # validate everything first
        conv = await self.ensure_conv(key)
        if "cwd" in new and new["cwd"] != conv.cwd and "session_id" not in new:
            new["session_id"] = ""  # sessions belong to a folder, like /cd
        for name, value in new.items():
            setattr(conv, name, value)
        if new.get("autoresume") is False:
            conv.resume_at = 0
        self.state.save()
        shown = ", ".join(f"{k}={v!r}" for k, v in new.items()) or "nothing changed"
        return f"conversation {key}: {shown}"

    async def apply_prompt(self, key: int, data: dict) -> str:
        text = str(data.get("text") or "")
        if not text.strip():
            raise RequestError("empty prompt")
        channel = await self.channel_by_id(key)
        if channel is None:
            raise RequestError(f"SEELE can't see channel {key}")
        await self.ensure_conv(key)
        name = str(data.get("as") or "").strip()
        new_session = bool(data.get("new_session"))
        head = f"📨 **Prompt from {name or 'seele prompt'}**" + (" · fresh session" if new_session else "")
        if key in self.jobs:
            head += " · after the current run"
        try:
            if len(text) <= 1800:
                await channel.send(f"{head}\n{text}")
            else:
                await channel.send(f"{head} (attached)", file=discord.File(io.BytesIO(text.encode()), "prompt.md"))
        except discord.HTTPException as e:
            log.warning("could not show the prompt in %s: %s", key, e)
        where = self.enqueue(key, channel, tag_prompt(name, text) if name else text,
                             human=False, new_session=new_session)
        return f"conversation {key}: prompt {'started' if where == 'started' else 'queued until the current run ends'}"


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
        if conv.agents_channel:
            lines.append(f"**Agent cards** <#{conv.agents_channel}>")
        if conv.autoresume:
            lines.append("**Auto-resume** on" + (f", next at {when_text(conv.resume_at)}" if conv.resume_at else ""))
        if job:
            queued = len(bot.queue.get(interaction.channel_id, []))
            running = len(job.tracker.running()) if job.tracker else 0
            lines.append(f"**Running** {duration(time.monotonic() - job.started)}, {job.tools} tools"
                         + (f", {job.results} replies" if job.results else "")
                         + (f", {running} agents" if running else "")
                         + (f", {queued} queued" if queued else ""))
        lines.append(f"**Busy here** {len(bot.jobs)} prompt(s) running across all chats")
        if await bot.guard_active():
            lines.append(f"🎮 `{bot.cfg.guard_process}` is running: Claude is told to keep it light")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @command("stop", "Stop the running prompt here, drop queued messages and any scheduled auto-resume")
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

    @command("agents", "Post a live card per background agent in a channel (empty = list them in the status)")
    @app_commands.describe(channel="Channel for the agent cards; leave empty to turn cards off")
    async def agents_cmd(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not await guard(interaction):
            return
        conv = conv_of(interaction)
        conv.agents_channel = str(channel.id) if channel else ""
        bot.state.save()
        await interaction.response.send_message(
            f"🤖 Agent cards for this conversation go to {channel.mention}." if channel
            else "🤖 Agents show up in the live status here.")

    @command("autoresume", "After a usage limit, prompt Claude again automatically when it resets")
    async def autoresume(interaction: discord.Interaction, on: bool = True):
        if not await guard(interaction):
            return
        conv = conv_of(interaction)
        conv.autoresume = on
        if not on:
            conv.resume_at = 0
        bot.state.save()
        await interaction.response.send_message(
            "⏸ Auto-resume on: after a usage limit, this conversation carries on when it resets." if on
            else "Auto-resume off.")

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
