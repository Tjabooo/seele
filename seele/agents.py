"""Subagent cards: every background agent Claude starts gets a live card in the conversation's
agents channel, plus a thread on that card with its prompt, its steps and its final report.

Without an agents channel the agents only show up as short lines in the live status.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import asdict, dataclass, field

import discord

from .text import duration, split_message

log = logging.getLogger("seele.agents")

# status -> (dot, label, embed colour)
STATUS = {
    "running": ("🟠", "Running", 0xE8590C),
    "done": ("🟢", "Done", 0x2F9E44),
    "failed": ("🔴", "Failed", 0xE03131),
    "stopped": ("⚪", "Stopped", 0x868E96),
}
WOKEN = "<task-notification>"  # prompt of an agent resumed because its own background task ended


def is_agent(task_type: str | None) -> bool:
    """Agents (local_agent, remote_agent, ...), not background shells or monitors."""
    return "agent" in (task_type or "")


def card_status(status: str | None) -> str:
    s = (status or "").lower()
    if s in ("running", "pending", "started"):
        return "running"
    if s in ("completed", "complete", "success", "done"):
        return "done"
    if s in ("failed", "error", "errored"):
        return "failed"
    return "stopped"  # killed, stopped, cancelled, ...


def tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class Card:
    """One agent, persisted in the state file by task id (a resumed agent keeps its card)."""
    task_id: str
    conv: int = 0
    description: str = ""
    subagent_type: str = ""
    status: str = "running"
    runs: int = 1
    started: float = 0.0  # epoch seconds, first start
    ended: float = 0.0
    tools: int = 0
    tokens: int = 0
    activity: str = ""
    channel_id: int = 0
    message_id: int = 0
    thread_id: int = 0
    jump: str = ""  # link back to the conversation

    @classmethod
    def load(cls, data: dict) -> "Card":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def elapsed(self, now: float) -> float:
        return max(0.0, (self.ended or now) - self.started)

    def stats(self, now: float) -> str:
        return (f"{duration(self.elapsed(now))} · {self.tools} tool{'s' * (self.tools != 1)} · "
                f"{tokens(self.tokens)} tokens")


def card_embed(card: Card, now: float) -> discord.Embed:
    dot, label, color = STATUS.get(card.status, STATUS["stopped"])
    if card.status == "running" and card.runs > 1:
        label = "Running again"
    head = f"{dot} **{label}** · `{card.subagent_type or 'agent'}`"
    if card.runs > 1:
        head += f" · run {card.runs}"
    lines = [head]
    if card.status == "running":
        lines.append(f"Started <t:{int(card.started)}:R> · {card.tools} tool{'s' * (card.tools != 1)} · "
                     f"{tokens(card.tokens)} tokens")
        if card.activity:
            lines.append(f"▸ {clip(card.activity, 200)}")
    else:
        lines.append(f"Took {card.stats(now)}")
    if card.jump:
        lines.append(f"[↩ Conversation]({card.jump})")
    return discord.Embed(title=clip("🤖 " + (card.description or card.task_id), 250),
                         description="\n".join(lines), color=color)


def compact_line(card: Card, now: float) -> str:
    """One line for the live status when there is no agents channel."""
    line = (f"🤖 {clip(card.description or card.task_id, 50)} · {card.subagent_type or 'agent'} · "
            f"{duration(card.elapsed(now))} · {card.tools} tool{'s' * (card.tools != 1)}")
    if card.activity:
        line += f" · {clip(card.activity, 60)}"
    return line[:160]


@dataclass
class Live:
    """In-memory bits of a card: buffered thread lines and rate-limit bookkeeping."""
    pending: list[str] = field(default_factory=list)
    last_post: float = 0.0
    last_edit: float = 0.0
    dirty: bool = False
    last_text: str = ""  # the agent's latest full text block
    report: str = ""  # what it handed back (SubagentHandback), if it used that tool
    message: object = None
    thread: object = None


class Board:
    """All agent cards of the bot. `resolve(channel_id)` returns a channel/thread or None."""

    def __init__(self, state, resolve, *, post_every: float = 12.0, edit_every: float = 10.0):
        self.state, self.resolve = state, resolve
        self.post_every, self.edit_every = post_every, edit_every
        self.live: dict[str, Live] = {}
        self.active: set[str] = set()  # task ids of agents in running jobs

    def keep(self, card: Card, save: bool = True):
        if card.channel_id:
            self.state.agents[card.task_id] = asdict(card)
            if save:
                self.state.save()

    async def message(self, card: Card):
        live = self.live.setdefault(card.task_id, Live())
        if live.message is None and card.channel_id and card.message_id:
            channel = await self.resolve(card.channel_id)
            if channel is not None:
                live.message = channel.get_partial_message(card.message_id)
        return live.message

    async def thread(self, card: Card):
        live = self.live.setdefault(card.task_id, Live())
        if live.thread is None and card.thread_id:
            live.thread = await self.resolve(card.thread_id)
        return live.thread

    async def thread_send(self, card: Card, content: str, file: tuple[str, str] | None = None):
        thread = await self.thread(card)
        if thread is None:
            return
        extra = {"file": discord.File(io.BytesIO(file[1].encode()), file[0])} if file else {}
        try:
            await thread.send(content[:2000], **extra)
        except discord.HTTPException as e:
            log.warning("agent thread %s: %s", card.thread_id, e)

    async def send_text(self, card: Card, text: str, filename: str):
        """Short text as a message, long text as a preview plus the whole thing as a file."""
        text = text.strip()
        if not text:
            return
        if len(text) <= 1900:
            await self.thread_send(card, text)
        else:
            await self.thread_send(card, text[:1500].rstrip() + "\n-# …full text attached", (filename, text))

    async def edit(self, card: Card):
        live = self.live.setdefault(card.task_id, Live())
        live.dirty, live.last_edit = False, time.monotonic()
        msg = await self.message(card)
        if msg is None:
            return
        try:
            await msg.edit(embed=card_embed(card, time.time()))
        except discord.HTTPException as e:
            log.warning("agent card %s: %s", card.message_id, e)

    async def flush(self, card: Card, force: bool = False):
        live = self.live.get(card.task_id)
        if live is None or not card.channel_id:
            return
        now = time.monotonic()
        if live.pending and (force or now - live.last_post >= self.post_every):
            lines, live.pending = live.pending, []
            live.last_post = now
            pieces = split_message("\n".join(lines), 1900)
            if len(pieces) > 3:  # a burst: one message with a file instead of a wall of posts
                await self.thread_send(card, pieces[0] + "\n-# …more steps attached",
                                       ("steps.md", "\n".join(lines)))
            else:
                for piece in pieces:
                    await self.thread_send(card, piece)
        if live.dirty and (force or now - live.last_edit >= self.edit_every):
            await self.edit(card)

    async def orphans(self):
        """After a restart: cards still marked running belong to a process that is gone."""
        changed = False
        for task_id, data in list(self.state.agents.items()):
            if data.get("status") != "running" or task_id in self.active:
                continue
            card = Card.load(data)
            card.status, card.ended = "stopped", card.ended or time.time()
            self.keep(card, save=False)
            changed = True
            await self.edit(card)
            await self.thread_send(card, "⏹ **Stopped**: SEELE restarted while this agent ran.")
            self.live.pop(task_id, None)
        if changed:
            self.state.save()


class Tracker:
    """The agents of one running job. Feed it the run's task and subagent events."""

    def __init__(self, board: Board, key: int, conv, jump: str = ""):
        self.board, self.key, self.conv, self.jump = board, key, conv, jump
        self.cards: dict[str, Card] = {}
        self.by_tool: dict[str, str] = {}  # Agent tool_use id -> task id

    def channel_id(self) -> int:
        try:
            return int(self.conv.agents_channel or 0)
        except (TypeError, ValueError):
            return 0

    def running(self) -> list[Card]:
        return [c for c in self.cards.values() if c.status == "running"]

    async def handle(self, ev) -> None:
        if ev.agent:
            return await self._step(ev)
        handler = {"task_started": self._started, "task_progress": self._progress,
                   "task_updated": self._updated, "task_notification": self._notification}.get(ev.kind)
        if handler:
            await handler(ev.data)

    async def _started(self, d: dict):
        if not is_agent(d.get("task_type")) or not d.get("task_id"):
            return
        tid = d["task_id"]
        if d.get("tool_use_id"):
            self.by_tool[d["tool_use_id"]] = tid
        self.board.active.add(tid)
        live = self.board.live.setdefault(tid, Live())
        live.report = live.last_text = ""
        prompt = d.get("prompt") or ""
        card = self.cards.get(tid)
        if card is None and tid in self.board.state.agents:
            card = Card.load(self.board.state.agents[tid])
        if card is None:  # a new agent
            card = Card(tid, conv=self.key, description=d.get("description") or "agent",
                        subagent_type=d.get("subagent_type") or "", started=time.time(), jump=self.jump)
            self.cards[tid] = card
            await self._post_card(card, prompt)
            return
        # the same agent again (SendMessage, or woken by its own background task): reuse its card
        self.cards[tid] = card
        card.runs += 1
        card.status, card.ended, card.activity = "running", 0.0, ""
        card.description = d.get("description") or card.description
        if not card.message_id:
            await self._post_card(card, prompt)
            return
        live.dirty = True
        await self.board.edit(card)
        await self._post_prompt(card, prompt, again=True)
        self.board.keep(card)

    async def _post_card(self, card: Card, prompt: str):
        cid = self.channel_id()
        if not cid:
            return
        channel = await self.board.resolve(cid)
        if channel is None:
            log.warning("agents channel %s not found", cid)
            return
        try:
            msg = await channel.send(embed=card_embed(card, time.time()))
        except discord.HTTPException as e:
            log.warning("could not post an agent card in %s: %s", cid, e)
            return
        live = self.board.live.setdefault(card.task_id, Live())
        card.channel_id, card.message_id = cid, msg.id
        live.message, live.last_edit = msg, time.monotonic()
        try:
            thread = await msg.create_thread(name=clip(card.description or "agent", 95),
                                             auto_archive_duration=10080)
            card.thread_id, live.thread = thread.id, thread
        except (discord.HTTPException, AttributeError) as e:
            log.warning("could not open a thread on agent card %s: %s", msg.id, e)
        self.board.keep(card)
        await self._post_prompt(card, prompt)

    async def _post_prompt(self, card: Card, prompt: str, again: bool = False):
        head = "↻ **Running again**" if again else "📝 **Prompt**"
        prompt = prompt.strip()
        if prompt.startswith(WOKEN):
            await self.board.thread_send(card, f"{head}: woken up by one of its background tasks.")
        elif not prompt:
            if again:
                await self.board.thread_send(card, head)
        elif len(prompt) <= 1800:
            await self.board.thread_send(card, f"{head}\n{prompt}")
        else:
            await self.board.thread_send(card, f"{head} (attached)", ("prompt.md", prompt))

    async def _step(self, ev):
        tid = self.by_tool.get(ev.agent)
        card = self.cards.get(tid) if tid else None
        if card is None:
            return
        live = self.board.live.setdefault(tid, Live())
        if ev.kind == "text":
            live.last_text = ev.text
            if card.thread_id:
                live.pending.append(ev.text if len(ev.text) <= 1500 else ev.text[:1500] + " …")
        elif ev.kind == "tool":
            if ev.data.get("name") == "SubagentHandback":
                report = (ev.data.get("input") or {}).get("message")
                if isinstance(report, str):
                    live.report = report
            if card.thread_id:
                live.pending.append("-# ▸ " + discord.utils.escape_markdown(ev.text))

    def _card(self, d: dict) -> Card | None:
        return self.cards.get(d.get("task_id") or "")

    async def _progress(self, d: dict):
        card = self._card(d)
        if card is None:
            return
        usage = d.get("usage") or {}
        card.tools = int(usage.get("tool_uses") or card.tools)
        card.tokens = int(usage.get("total_tokens") or card.tokens)
        activity = d.get("description") or ""
        card.activity = "" if activity == card.description else activity
        self.board.live.setdefault(card.task_id, Live()).dirty = True

    async def _updated(self, d: dict):
        card = self._card(d)
        status = (d.get("patch") or {}).get("status")
        if card is None or not status or card_status(status) == "running":
            return
        card.status = card_status(status)
        end = (d.get("patch") or {}).get("end_time")
        card.ended = float(end) / 1000 if end else time.time()
        self.board.live.setdefault(card.task_id, Live()).dirty = True

    async def _notification(self, d: dict):
        card = self._card(d)
        if card is None:
            return
        now = time.time()
        card.status = card_status(d.get("status"))
        card.ended = card.ended or now
        card.activity = ""
        usage = d.get("usage") or {}
        card.tools = int(usage.get("tool_uses") or card.tools)
        card.tokens = int(usage.get("total_tokens") or card.tokens)
        live = self.board.live.setdefault(card.task_id, Live())
        live.dirty = True
        if card.channel_id:
            await self.board.flush(card, force=True)
            dot, label, _ = STATUS[card.status]
            await self.board.thread_send(card, f"{dot} **{label}** · {card.stats(now)}")
            summary = (d.get("summary") or "").strip()
            if live.report.strip():
                await self.board.send_text(card, live.report, "report.md")
            else:
                if summary:
                    await self.board.send_text(card, summary, "summary.md")
                if live.last_text.strip() and live.last_text.strip() != summary:
                    await self.board.send_text(card, live.last_text, "last-message.md")
            self.board.keep(card)
        live.report = live.last_text = ""

    async def flush(self, force: bool = False):
        for card in list(self.cards.values()):
            await self.board.flush(card, force)

    async def close(self):
        """The job ended: agents still running died with the process."""
        now = time.time()
        for card in list(self.cards.values()):
            live = self.board.live.setdefault(card.task_id, Live())
            if card.status == "running":
                card.status, card.ended, card.activity = "stopped", now, ""
                live.dirty = True
                if card.thread_id:
                    live.pending.append("⏹ **Stopped**: the Claude process ended before this agent finished.")
            await self.board.flush(card, force=True)
            self.board.keep(card, save=False)
            self.board.active.discard(card.task_id)
            self.board.live.pop(card.task_id, None)
        if any(c.channel_id for c in self.cards.values()):
            self.board.state.save()
