"""Drive whole prompts through the bot with a fake `claude` and fake Discord channels."""
import asyncio
import itertools
import json
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from seele import agents, claude
from seele import bot as botmod
from seele import config
from seele.__main__ import write_request
from seele.bot import Seele

FAKE = r'''#!PYTHON
import json, queue, sys, threading, time
argv = sys.argv[1:]
live = "--input-format" in argv
sid = argv[argv.index("--resume") + 1] if "--resume" in argv else "sess-1"
if sid == "gone":
    print("No conversation found with session ID: gone", file=sys.stderr)
    sys.exit(1)
log = open(sys.argv[0] + ".log", "a")
q = queue.Queue()
eof = False

def reader():
    if live:
        for line in sys.stdin:
            q.put(json.loads(line)["message"]["content"])
    else:
        q.put(sys.stdin.read())
    q.put(None)

threading.Thread(target=reader, daemon=True).start()

def out(**d):
    print(json.dumps(d), flush=True)

def nxt(timeout=None):
    global eof
    item = q.get(timeout=timeout)
    if item is None:
        eof = True
    return item

def note(prompt):
    log.write(json.dumps({"argv": argv, "prompt": prompt}) + "\n")
    log.flush()

def agent_scenario(prompt):
    out(type="assistant", message={"content": [{"type": "tool_use", "id": "tu1", "name": "Agent",
        "input": {"description": "tiny job", "prompt": "do the tiny job"}}]})
    out(type="system", subtype="task_started", task_id="t1", tool_use_id="tu1", description="tiny job",
        subagent_type="general-purpose", task_type="local_agent", prompt="do the tiny job")
    out(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "t1", "task_type": "local_agent"}])
    out(type="assistant", parent_tool_use_id="tu1", message={"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}]})
    out(type="system", subtype="task_progress", task_id="t1", description="Running echo hi",
        usage={"total_tokens": 1500, "tool_uses": 1})
    out(type="assistant", parent_tool_use_id="tu1", message={"content": [{"type": "text", "text": "inner commentary"}]})
    out(type="result", subtype="success", is_error=False, result="STARTED", session_id=sid)
    try:  # the test writes a message in now
        m = nxt(timeout=0.3 if "quick" in prompt else 8)
    except queue.Empty:
        m = None
    if m:
        note(m)
        out(type="system", subtype="init", session_id=sid, model="fake", cwd=".")
        out(type="result", subtype="success", is_error=False, result="got: " + m.splitlines()[-1], session_id=sid)
    out(type="assistant", parent_tool_use_id="tu1", message={"content": [
        {"type": "tool_use", "name": "SubagentHandback", "input": {"message": "the full report"}}]})
    out(type="system", subtype="task_updated", task_id="t1", patch={"status": "completed", "end_time": time.time() * 1000})
    out(type="system", subtype="task_notification", task_id="t1", status="completed",
        summary="This agent's report was delivered to you as a message", usage={"total_tokens": 2000, "tool_uses": 2})
    out(type="system", subtype="background_tasks_changed", tasks=[])
    out(type="system", subtype="init", session_id=sid, model="fake", cwd=".")
    out(type="result", subtype="success", is_error=False, result="AGENT-DONE", session_id=sid)
    out(type="system", subtype="init", session_id=sid, model="fake", cwd=".")
    out(type="result", subtype="success", is_error=False, result="", session_id=sid)

# the summary of a task_notification as stream-json sends it for an agent stopped by the limit
LIMIT_NOTE = ("Agent terminated early due to an API error: You've hit your 5-hour limit · resets 10:10pm "
              "(Europe/Stockholm) (error type rate_limit, HTTP 429, request id req_x, model sent to the API: "
              "claude-opus-5-5)")
OTHER_NOTE = "Agent terminated early due to an API error: Overloaded (error type overloaded_error, HTTP 529)"

def agent_limit_scenario(prompt):
    # agent ae46... is stopped by the 5-hour limit; the lead's own turn ends normally. With STAY a
    # second agent keeps the process alive (stdin open), waiting for the next message.
    agents = [("tu2", "ae4690131b5af3ed0", "SB1: Stubby look-dev r01")]
    if "STAY" in prompt:
        agents.append(("tu3", "a0000000000000003", "slow one"))
    for tu, tid, desc in agents:
        out(type="assistant", message={"content": [{"type": "tool_use", "id": tu, "name": "Agent",
            "input": {"description": desc, "prompt": "work", "run_in_background": True}}]})
        out(type="system", subtype="task_started", task_id=tid, tool_use_id=tu, description=desc,
            subagent_type="general-purpose", task_type="local_agent", prompt="work")
    out(type="system", subtype="background_tasks_changed",
        tasks=[{"task_id": tid, "task_type": "local_agent"} for _, tid, _ in agents])
    out(type="system", subtype="task_updated", task_id="ae4690131b5af3ed0", patch={"status": "failed"})
    out(type="system", subtype="task_notification", task_id="ae4690131b5af3ed0", status="failed",
        summary=OTHER_NOTE if "OTHERFAIL" in prompt else LIMIT_NOTE)
    out(type="system", subtype="background_tasks_changed",
        tasks=[{"task_id": tid, "task_type": "local_agent"} for _, tid, _ in agents[1:]])
    out(type="result", subtype="success", is_error=False, result="STARTED", session_id=sid)
    if "STAY" not in prompt:
        return
    m = nxt(timeout=8)
    if m:
        note(m)
        out(type="system", subtype="init", session_id=sid, model="fake", cwd=".")
        out(type="result", subtype="success", is_error=False, result="got: " + m.splitlines()[-1], session_id=sid)
    out(type="system", subtype="task_notification", task_id="a0000000000000003", status="completed", summary="ok")
    out(type="system", subtype="background_tasks_changed", tasks=[])
    out(type="system", subtype="init", session_id=sid, model="fake", cwd=".")
    out(type="result", subtype="success", is_error=False, result="", session_id=sid)

def turn(prompt):
    note(prompt)
    out(type="system", subtype="init", session_id=sid, model="fake", cwd=".")
    out(type="assistant", message={"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]})
    out(type="rate_limit_event", rate_limit_info={"status": "allowed", "overageStatus": "rejected",
        "unifiedWindows": {"five_hour": {"utilization": 0.25, "status": "allowed"}}})
    if "LIMIT" in prompt and "AGENTLIMIT" not in prompt:
        out(type="rate_limit_event", rate_limit_info={"status": "rejected", "resetsAt": 2000000000,
            "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": 1999990000, "status": "rejected"},
                               "seven_day": {"utilization": 1.0, "resetsAt": 2000000000, "status": "rejected"}}})
        out(type="result", subtype="success", is_error=True, result="You've hit your limit", session_id=sid)
    elif "AGENTLIMIT" in prompt:
        agent_limit_scenario(prompt)
    elif "AGENT" in prompt:
        agent_scenario(prompt)
    else:
        out(type="result", subtype="success", is_error=False, result="answer: " + prompt, session_id=sid,
            num_turns=2, total_cost_usd=0.01)

turn(nxt())
while not eof:
    m = nxt()
    if m is None:
        break
    turn(m)
'''

ids = itertools.count(1000)


class Thread:
    def __init__(self, name):
        self.id, self.name, self.sent = next(ids), name, []

    async def send(self, content=None, file=None, **kw):
        self.sent.append((content, file))
        return Msg(self, content)


class Msg:
    def __init__(self, channel, content, view=None, embed=None):
        self.id = next(ids)
        self.channel, self.content, self.view, self.embed = channel, content, view, embed
        self.deleted, self.thread, self.edits = False, None, 0

    async def edit(self, content=None, view=None, embed=None):
        if content is not None:
            self.content = content
        if embed is not None:
            self.embed = embed
        self.view = view
        self.edits += 1

    async def delete(self):
        self.deleted = True

    async def create_thread(self, name, auto_archive_duration=None):
        self.thread = Thread(name)
        self.channel.threads[self.thread.id] = self.thread
        return self.thread


class Typing:
    def __await__(self):
        return asyncio.sleep(0).__await__()


class Channel:
    def __init__(self, cid=1):
        self.id, self.sent, self.threads = cid, [], {}
        self.jump_url = f"https://discord.com/channels/@me/{cid}"

    async def send(self, content=None, view=None, file=None, embed=None):
        m = Msg(self, content, view, embed)
        self.sent.append(m)
        return m

    def typing(self):
        return Typing()

    def get_partial_message(self, mid):
        return next(m for m in self.sent if m.id == mid)

    def live(self):
        return [m for m in self.sent if not m.deleted]


class ChatThread(Channel):
    def __init__(self, cid, parent_id):
        super().__init__(cid)
        self.parent_id = parent_id


@pytest.fixture
def bot(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(FAKE.replace("PYTHON", sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cfg = config.Config(claude_bin=str(fake), default_cwd=tmp_path, settings_file=tmp_path / "s.json")
    cfg.accounts = [config.Account("one", tmp_path / "a"), config.Account("two", tmp_path / "b")]
    cfg.channels = {"agents": 500}
    b = Seele(cfg, config.State(tmp_path / "state.json"))
    b.requests_dir = tmp_path / "requests"
    return b, fake


def calls(fake):
    path = fake.parent / "claude.log"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


async def until(cond, timeout=15):
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)


def test_prompt_then_resume(bot):
    b, fake = bot
    ch = Channel()

    async def go():
        b.submit(1, ch, "hello")
        assert 1 in b.jobs  # busy before the task even starts
        await until(lambda: not b.jobs)
        b.submit(1, ch, "again")
        await until(lambda: not b.jobs)
    asyncio.run(go())
    status, reply = ch.sent[0], ch.sent[1]
    assert status.content.startswith("✅ **Done**") and "1 tool" in status.content and "25%" in status.content
    assert status.view is None
    assert reply.content == "answer: hello"
    first, second = calls(fake)
    assert "--resume" not in first["argv"] and first["prompt"] == "hello"
    assert "--input-format" in first["argv"]  # live input is the default
    assert second["argv"][second["argv"].index("--resume") + 1] == "sess-1"
    assert b.state.get(1).session_id == "sess-1"


@pytest.mark.parametrize("configured_thread", [False, True])
def test_codex_thread_queues_and_resumes(bot, tmp_path, monkeypatch, configured_thread):
    b, _ = bot
    fake = tmp_path / "codex"
    fake.write_text(f"#!{sys.executable}\n" + r'''
import json, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
assert args[0] == "exec"
assert args[args.index("--model") + 1] == "gpt-6.1-sol"
assert 'model_reasoning_effort="xhigh"' in args
assert args[-1] == "-"
if prompt.endswith("again"):
    assert args[1] == "resume" and args[-2] == "codex-session"
else:
    assert "resume" not in args
for event in [
    {"type": "thread.started", "thread_id": "codex-session"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Working on it"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": prompt}},
    {"type": "turn.completed"},
]:
    print(json.dumps(event), flush=True)
''')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    ch = ChatThread(11, 1)
    monkeypatch.setattr(botmod.discord, "Thread", ChatThread)
    b.cfg.projects_root = tmp_path / "projects"
    b.cfg.codex = config.CodexConfig(binary=str(fake), channels={ch.id if configured_thread else ch.parent_id})
    b.owners = {42}
    author = SimpleNamespace(bot=False, id=42, display_name="Alice")
    reactions = []

    async def react(emoji):
        reactions.append(emoji)

    async def go():
        for prompt in ("hello", "again"):
            await b.on_message(SimpleNamespace(author=author, channel=ch, mentions=[], content=prompt,
                                               attachments=[], add_reaction=react))
        await until(lambda: not b.jobs)

    asyncio.run(go())
    assert reactions == ["📥"]
    assert not ch.threads
    assert [msg.content for msg in ch.live() if msg.content.startswith("[Discord")] == [
        "[Discord message from Alice]\nhello", "[Discord message from Alice]\nagain",
    ]
    assert all("gpt-6.1-sol · effort xhigh" in msg.content for msg in ch.live()[::2])
    assert config.State(b.state.path).get(ch.id).session_id == "codex-session"


def test_codex_channel_opens_fresh_threads_and_inherits_settings(bot, tmp_path, monkeypatch):
    b, _ = bot
    channel = Channel(7)
    b.cfg.codex.channels = {channel.id}
    b.owners = {42}
    parent = b.state.get(channel.id)
    parent.session_id, parent.cwd = "old-channel-session", str(tmp_path)
    parent.model, parent.effort = "custom-model", "high"
    parent.listen = True  # Codex channels require mentions even with an older listen setting.
    bot_user = SimpleNamespace(id=99)
    monkeypatch.setattr(b._connection, "user", bot_user)
    monkeypatch.setattr(botmod.discord, "Thread", ChatThread)
    delivered = []

    async def create_thread(name, auto_archive_duration):
        assert auto_archive_duration == 1440
        thread = ChatThread(next(ids), channel.id)
        channel.threads[thread.id] = thread
        return thread

    async def deliver(key, target, prompt):
        delivered.append((key, target, prompt))
        return "started"

    monkeypatch.setattr(b, "deliver", deliver)
    author = SimpleNamespace(bot=False, id=42, display_name="Alice")

    async def go():
        for content in ("ordinary conversation", "codex please do something", "<@123> hello"):
            await b.on_message(SimpleNamespace(author=author, channel=channel, mentions=[], content=content,
                                               attachments=[], create_thread=create_thread))
        assert not delivered and not channel.threads
        for content in ("first topic", "second topic"):
            await b.on_message(SimpleNamespace(author=author, channel=channel, mentions=[bot_user],
                                               content=f"<@99> {content}",
                                               attachments=[], create_thread=create_thread))
        thread = delivered[0][1]
        await b.on_message(SimpleNamespace(author=author, channel=thread, mentions=[], content="follow up",
                                           attachments=[]))
        b.get_channel = lambda key: channel if key == channel.id else channel.threads.get(key)
        with pytest.raises(botmod.RequestError, match="Codex chat settings"):
            await b.apply_conv(thread.id, {"account": "two"})
        await b.apply_conv(thread.id, {"effort": "xhigh"})

    asyncio.run(go())
    first, second, follow_up = delivered
    assert first[2] == "[Discord message from Alice]\nfirst topic"
    assert first[0] != second[0] and follow_up[0] == first[0]
    assert len(channel.threads) == 2
    for key in channel.threads:
        conv = config.State(b.state.path).get(key)
        assert conv.session_id == "" and conv.cwd == str(tmp_path) and conv.model == "custom-model"
        assert conv.effort == ("xhigh" if key == first[0] else "high")
    assert b.state.get(channel.id).session_id == "old-channel-session"


def test_thread_creation_failure_does_not_start_a_shared_session(bot, monkeypatch):
    b, fake = bot
    channel = Channel(7)
    b.cfg.codex.channels = {channel.id}
    b.owners = {42}
    bot_user = SimpleNamespace(id=99)
    monkeypatch.setattr(b._connection, "user", bot_user)

    async def create_thread(**kwargs):
        raise botmod.discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")

    msg = SimpleNamespace(author=SimpleNamespace(bot=False, id=42), channel=channel, mentions=[bot_user],
                          content="new topic", attachments=[], create_thread=create_thread)
    asyncio.run(b.on_message(msg))
    assert not b.jobs and not calls(fake)
    assert "Couldn't create a chat thread" in channel.sent[0].content


def test_codex_thread_commands_use_inherited_settings(bot, tmp_path, monkeypatch):
    b, _ = bot
    parent = Channel(7)
    thread = ChatThread(8, parent.id)
    b.cfg.codex.channels = {parent.id}
    b.owners = {42}
    conv = b.state.get(parent.id)
    conv.cwd, conv.effort = str(tmp_path), "high"
    b.get_channel = lambda key: {parent.id: parent, thread.id: thread}.get(key)
    monkeypatch.setattr(botmod.discord, "Thread", ChatThread)
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(channel_id=thread.id, channel=thread, user=SimpleNamespace(id=42),
                                  response=response)

    async def go():
        await b.tree.get_command("status").callback(interaction)
        shown = response.send_message.call_args.args[0]
        assert "**Backend** Codex" in shown and "**Effort** high" in shown and str(tmp_path) in shown
        await b.tree.get_command("model").callback(interaction, "default")
        assert b.cfg.codex.model in response.send_message.call_args.args[0]
        await b.tree.get_command("effort").callback(interaction, SimpleNamespace(value="xhigh"))
        assert b.state.get(thread.id).effort == "xhigh"
        await b.tree.get_command("account").callback(interaction, "two")
        assert response.send_message.call_args.args[0] == "This command is for Claude chats."

    asyncio.run(go())


def test_plain_stdin_when_live_input_is_off(bot):
    b, fake = bot
    b.cfg.live_input = False
    ch = Channel()

    async def go():
        b.submit(1, ch, "hello")
        await until(lambda: not b.jobs)
        assert await b.deliver(1, ch, "later") == "started"
        await until(lambda: not b.jobs)
    asyncio.run(go())
    assert "--input-format" not in calls(fake)[0]["argv"]
    assert [m.content for m in ch.sent if m.content.startswith("answer")] == ["answer: hello", "answer: later"]


def test_live_input_agent_cards_and_one_reply_per_turn(bot):
    b, fake = bot
    ch, agents_ch = Channel(1), Channel(500)
    b.get_channel = lambda cid: {500: agents_ch, **agents_ch.threads}.get(cid)
    b.state.get(1).agents_channel = "500"
    where = {}

    async def go():
        b.submit(1, ch, "AGENT please")
        await until(lambda: any(m.content == "STARTED" for m in ch.sent))
        where["mid"] = await b.deliver(1, ch, "[Discord message from Tester]\nhello mid-run")
        await until(lambda: not b.jobs)
    asyncio.run(go())

    assert where["mid"] == "live"
    assert calls(fake)[1]["prompt"].endswith("hello mid-run")  # went into the same process
    texts = [m.content for m in ch.live()]
    replies = [t for t in texts if not t.startswith(("⏳", "💤", "✅", "⚠️"))]
    assert replies == ["STARTED", "got: hello mid-run", "AGENT-DONE"]  # the empty last turn isn't posted
    statuses = [m for m in ch.live() if m.content.startswith(("✅", "⏳", "💤"))]
    assert len(statuses) == 1 and statuses[0].content.startswith("✅ **Done**") and "3 replies" in statuses[0].content
    assert ch.sent.index(statuses[0]) > ch.sent.index(next(m for m in ch.sent if m.content == "STARTED"))
    assert any(m.deleted for m in ch.sent)  # the status moved below a reply

    card = agents_ch.sent[0]
    assert "tiny job" in card.embed.title and "Done" in card.embed.description
    assert "general-purpose" in card.embed.description and "2 tools" in card.embed.description
    thread = [m for m, _ in card.thread.sent]
    assert thread[0] == "📝 **Prompt**\ndo the tiny job"
    joined = "\n".join(thread)
    assert "▸ Bash: echo hi" in joined and "inner commentary" in joined
    assert "🟢 **Done**" in joined and "the full report" in joined
    saved = b.state.agents["t1"]
    assert saved["status"] == "done" and saved["message_id"] == card.id and saved["thread_id"] == card.thread.id


def test_resumed_agent_reuses_its_card(bot):
    b, _ = bot
    ch, agents_ch = Channel(1), Channel(500)
    b.get_channel = lambda cid: {500: agents_ch, **agents_ch.threads}.get(cid)
    b.state.get(1).agents_channel = "500"

    async def go():
        b.submit(1, ch, "AGENT once quick")
        await until(lambda: not b.jobs)
        b.board.live.clear()  # as after a restart: only the state file remembers the card
        b.submit(1, ch, "AGENT twice quick")
        await until(lambda: not b.jobs)
    asyncio.run(go())
    assert len(agents_ch.sent) == 1  # one card for task t1, edited, not a second one
    card = agents_ch.sent[0]
    assert b.state.agents["t1"]["runs"] == 2
    assert any((m or "").startswith("↻ **Running again**") for m, _ in card.thread.sent)


def test_agents_in_status_without_channel(bot):
    b, _ = bot
    ch = Channel()
    seen = []

    async def go():
        b.submit(1, ch, "AGENT please")
        await until(lambda: any(m.content == "STARTED" for m in ch.sent))
        seen.append(b.render(b.jobs[1]))
        await b.deliver(1, ch, "x")
        await until(lambda: not b.jobs)
    asyncio.run(go())
    assert "🤖 tiny job · general-purpose" in seen[0] and "Running echo hi" in seen[0]
    assert "💤 **Waiting**" in seen[0]
    assert not b.state.agents  # no channel, nothing persisted


def test_message_after_stdin_closed_is_queued(bot):
    b, fake = bot
    ch = Channel()
    where = []
    original = b.on_result

    async def hooked(key, job, ev, account):  # a message lands right after the last answer
        await original(key, job, ev, account)
        if not where:
            where.append(await b.deliver(1, ch, "late"))
    b.on_result = hooked

    async def go():
        b.submit(1, ch, "hello")
        await until(lambda: not b.jobs and len(calls(fake)) == 2)
    asyncio.run(go())
    assert where == ["queued"]  # stdin was already closed: it waits for the next run
    assert [c["prompt"] for c in calls(fake)] == ["hello", "late"]
    assert "--resume" in calls(fake)[1]["argv"]


def test_limit_offers_other_account_and_schedules_autoresume(bot):
    b, fake = bot
    ch = Channel()
    b.state.get(2).autoresume = True

    async def go():
        b.submit(2, ch, "LIMIT please")
        await until(lambda: not b.jobs)
    asyncio.run(go())
    assert ch.sent[0].content.startswith("⚠️ **Failed**")
    reply = ch.sent[1]
    assert "hit your limit" in reply.content
    assert [i.label for i in reply.view.children] == ["Retry on two"]
    # the later of the two exhausted windows (5-hour and weekly), plus 5 minutes
    assert b.state.get(2).resume_at == 2000000000 + 300
    assert any(m.content.startswith("⏸ Usage limit hit; resuming automatically at <t:2000000300:")
               for m in ch.sent)


def test_autoresume_fires_and_human_prompt_cancels(bot):
    b, fake = bot
    ch = Channel(7)
    b.get_channel = lambda cid: ch if cid == 7 else None
    conv = b.state.get(7)

    async def go():
        conv.resume_at = time.time() - 1
        await b.check_resumes()
        await until(lambda: not b.jobs)
        conv.resume_at = time.time() + 3600
        b.submit(7, ch, "a human")
        await until(lambda: not b.jobs)
    asyncio.run(go())
    assert calls(fake)[0]["prompt"] == botmod.AUTORESUME_PROMPT
    assert any(m.content.startswith("▶️") for m in ch.sent)
    assert conv.resume_at == 0
    assert any("Auto-resume cancelled" in m.content for m in ch.sent)


def test_old_session_retries_fresh(bot):
    b, fake = bot
    ch = Channel()
    b.state.get(4).session_id = "gone"

    async def go():
        b.submit(4, ch, "hi")
        await until(lambda: not b.jobs)
    asyncio.run(go())
    assert "Old session not found" in ch.sent[0].content
    assert any(m.content == "answer: hi" for m in ch.sent)
    assert b.state.get(4).session_id == "sess-1"


def test_missing_folder_frees_the_chat(bot, tmp_path):
    b, _ = bot
    b.state.get(3).cwd = str(tmp_path / "nope")
    ch = Channel()

    async def go():
        b.submit(3, ch, "hi")
        await until(lambda: not b.jobs)
    asyncio.run(go())
    assert "doesn't exist" in ch.sent[0].content


def test_requests_spool_conv_and_prompt(bot, tmp_path):
    b, fake = bot
    ch = Channel(9)
    b.get_channel = lambda cid: ch if cid == 9 else None
    proj = tmp_path / "proj"
    proj.mkdir()
    conv = b.state.get(9)
    conv.session_id = "old"

    async def go():
        ok = write_request({"kind": "conv", "id": 9, "set": {
            "cwd": str(proj), "effort": "xhigh", "agents_channel": "agents", "autoresume": "on",
            "session": "sess-1", "account": "two"}}, b.requests_dir)
        bad = write_request({"kind": "conv", "id": 9, "set": {"effort": "huge", "model": "x"}}, b.requests_dir)
        await b.poll_requests()
        answers = [json.loads(p.with_suffix(".done").read_text()) for p in (ok, bad)]
        assert answers[0]["ok"] and not answers[1]["ok"] and "effort" in answers[1]["message"]
        assert conv.model == ""  # a bad request changes nothing
        # a prompt while a job runs waits for it; --new-session starts the next one fresh
        b.submit(9, ch, "AGENT first")
        await until(lambda: any(m.content == "STARTED" for m in ch.sent))
        write_request({"kind": "prompt", "id": 9, "text": "hand over", "new_session": True, "as": "lead"},
                      b.requests_dir)
        await b.poll_requests()
        assert [p.prompt for p in b.queue[9]] == ["[Discord message from lead]\nhand over"]
        await b.deliver(9, ch, "nudge")
        await until(lambda: not b.jobs and len(calls(fake)) == 3)
    asyncio.run(go())
    assert (conv.cwd, conv.effort, conv.agents_channel, conv.autoresume, conv.account) == \
        (str(proj.resolve()), "xhigh", "500", True, "two")
    last = calls(fake)[-1]
    assert last["prompt"] == "[Discord message from lead]\nhand over"
    assert "--resume" not in last["argv"]  # the handover started a fresh session
    assert any(m.content.startswith("📨 **Prompt from lead** · fresh session") for m in ch.sent)


def test_stop_kills_a_live_run(bot):
    b, _ = bot
    ch = Channel()

    async def go():
        b.submit(1, ch, "AGENT please")
        await until(lambda: any(m.content == "STARTED" for m in ch.sent))
        assert b.stop(1)
        await until(lambda: not b.jobs)
    asyncio.run(go())
    final = [m for m in ch.live() if m.content.startswith(("⏹", "✅", "⚠️"))]
    assert final and final[-1].content.startswith("⏹ **Stopped**")


STOPPED = "ae4690131b5af3ed0"  # the agent id the lead's SendMessage takes (= its task id)
STOPPED_LINE = f"- `{STOPPED}`: SB1: Stubby look-dev r01 (general-purpose)"


def test_agent_limit_schedules_resume_and_names_the_agent_after_the_run_exits(bot):
    b, fake = bot
    ch, agents_ch = Channel(1), Channel(500)
    b.get_channel = lambda cid: {1: ch, 500: agents_ch, **agents_ch.threads}.get(cid)
    conv = b.state.get(1)
    conv.autoresume, conv.agents_channel = True, "500"

    async def go():
        b.submit(1, ch, "AGENTLIMIT go")
        await until(lambda: not b.jobs)  # the lead's turn ended normally and the process exited
        expected = claude.reset_from_text("resets 10:10pm (Europe/Stockholm)") + botmod.RESUME_DELAY
        assert abs(conv.resume_at - expected) < 5 and not conv.resume_guessed
        assert conv.resume_agents == [{"id": STOPPED, "description": "SB1: Stubby look-dev r01",
                                       "type": "general-purpose"}]
        assert b.state.agents[STOPPED]["status"] == "failed"
        b.state = config.State(b.state.path)  # survives a restart: it's in the state file
        b.board.state = b.state
        conv2 = b.state.get(1)
        assert conv2.resume_agents[0]["id"] == STOPPED
        conv2.resume_at = time.time() - 1  # the reset has passed
        await b.check_resumes()
        await until(lambda: not b.jobs)
        assert conv2.resume_at == 0 and conv2.resume_agents == []  # sent once, then cleared
    asyncio.run(go())
    announced = [m.content for m in ch.sent if m.content.startswith("⏸")]
    assert len(announced) == 1 and announced[0].startswith(
        "⏸ Background agents hit the usage limit; resuming automatically at <t:")
    assert any(m.content == "▶️ The usage limit has reset: resuming and picking up 1 stopped agent."
               for m in ch.sent)
    first, resumed = calls(fake)
    assert resumed["prompt"].startswith(botmod.AUTORESUME_PROMPT)
    assert resumed["prompt"].endswith(STOPPED_LINE)
    assert resumed["argv"][resumed["argv"].index("--resume") + 1] == "sess-1"


def test_agent_limit_resume_goes_into_the_running_session(bot):
    b, fake = bot
    ch = Channel(1)
    b.get_channel = lambda cid: ch if cid == 1 else None
    conv = b.state.get(1)
    conv.autoresume = True  # no agents channel: the cards only live in memory

    async def go():
        b.submit(1, ch, "AGENTLIMIT STAY")
        await until(lambda: any(m.content == "STARTED" for m in ch.sent))
        assert conv.resume_at and [a["id"] for a in conv.resume_agents] == [STOPPED]
        conv.resume_at = time.time() - 1
        await b.check_resumes()
        await until(lambda: not b.jobs)
    asyncio.run(go())
    prompts = [c["prompt"] for c in calls(fake)]
    assert len(prompts) == 2 and prompts[1].startswith(botmod.AUTORESUME_PROMPT)
    replies = [m.content for m in ch.live() if m.content.startswith(("got:", "answer:"))]
    assert replies == ["got: " + STOPPED_LINE]  # answered by the same process, not a second run
    assert any(m.content.startswith("▶️") and "sent into the running session" in m.content for m in ch.sent)


def test_agent_failure_without_a_limit_or_autoresume_schedules_nothing(bot):
    b, _ = bot
    ch = Channel(1)
    b.state.get(1).autoresume = True
    b.state.get(2).autoresume = False

    async def go():
        b.submit(1, ch, "AGENTLIMIT OTHERFAIL")  # failed, but not because of a limit
        await until(lambda: not b.jobs)
        b.submit(2, ch, "AGENTLIMIT go")  # a limit, but auto-resume is off
        await until(lambda: not b.jobs)
    asyncio.run(go())
    for key in (1, 2):
        assert b.state.get(key).resume_at == 0 and b.state.get(key).resume_agents == []
    assert not any((m.content or "").startswith("⏸") for m in ch.sent)


def test_resume_dedupes_later_reset_wins_and_stop_drops_it(bot):
    b, _ = bot
    ch = Channel(3)
    conv = b.state.get(3)
    conv.autoresume = True
    job = botmod.Job(ch, "x", key=3)
    acc = b.cfg.accounts[0]
    text = "Agent terminated early due to an API error: You've hit your 5-hour limit · resets 10:10pm (Europe/Stockholm)"

    async def go():
        for tid in ("a1", "a2", "a1"):  # two agents at the same limit, one of them reported twice
            await b.agent_limited(3, job, agents.Card(tid, description=f"job {tid}"), text, acc)
        first = conv.resume_at
        assert abs(first - claude.reset_from_text(text) - botmod.RESUME_DELAY) < 5
        # the lead's own limit at the same reset: already scheduled and announced
        await b.schedule_resume(3, job, claude.Event("result", text=text, is_error=True), acc)
        assert not await b.plan_resume(3, ch, None, "Usage limit hit")  # a guess never replaces a known time
        assert not await b.plan_resume(3, ch, first - 7200, "Usage limit hit")  # an earlier reset neither
        assert conv.resume_at == first
        assert await b.plan_resume(3, ch, first + 86400, "Usage limit hit")  # a later (weekly) reset wins
        assert conv.resume_at == first + 86400 + botmod.RESUME_DELAY
    asyncio.run(go())
    assert [a["id"] for a in conv.resume_agents] == ["a1", "a2"]
    prompt = botmod.resume_prompt(conv.resume_agents)
    assert prompt.startswith(botmod.AUTORESUME_PROMPT) and "- `a1`: job a1" in prompt and "- `a2`: job a2" in prompt
    assert len([m for m in ch.sent if m.content.startswith("⏸")]) == 2  # scheduled, then moved later
    assert not b.stop(3)  # nothing running, but /stop still drops the scheduled resume and its list
    assert conv.resume_at == 0 and conv.resume_agents == []


def test_human_prompt_cancelling_a_resume_carries_the_stopped_agents(bot):
    b, fake = bot
    ch = Channel(8)
    conv = b.state.get(8)
    conv.autoresume, conv.resume_at = True, time.time() + 3600
    conv.resume_agents = [{"id": "a9", "description": "nine", "type": ""}]

    async def go():
        b.submit(8, ch, "carry on")
        await until(lambda: not b.jobs)
    asyncio.run(go())
    [call] = calls(fake)
    assert call["prompt"].startswith("carry on\n\n[SEELE note] Agents the limit stopped") and "- `a9`: nine" in call["prompt"]
    assert conv.resume_at == 0 and conv.resume_agents == []
    assert any("Auto-resume cancelled" in m.content and "1 stopped agent" in m.content for m in ch.sent)
