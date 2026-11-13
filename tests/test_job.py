"""Drive a whole prompt through the bot with a fake `claude` and a fake Discord channel."""
import asyncio
import json
import stat
import sys

import pytest

from seele import config
from seele.bot import Seele

FAKE = r'''#!PYTHON
import json, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
sid = argv[argv.index("--resume") + 1] if "--resume" in argv else "sess-1"
with open(sys.argv[0] + ".log", "a") as f:
    f.write(json.dumps({"argv": argv, "prompt": prompt}) + "\n")
def out(**d): print(json.dumps(d), flush=True)
out(type="system", subtype="init", session_id=sid, model="fake", cwd=".")
out(type="assistant", message={"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]})
out(type="rate_limit_event", rate_limit_info={"unifiedWindows": {"five_hour": {"utilization": 0.25}}})
if "LIMIT" in prompt:
    out(type="result", subtype="success", is_error=True, result="You've hit your limit", session_id=sid)
else:
    out(type="result", subtype="success", is_error=False, result="answer: " + prompt, session_id=sid,
        num_turns=2, total_cost_usd=0.01)
'''


class Msg:
    def __init__(self, channel, content, view=None):
        self.channel, self.content, self.view = channel, content, view

    async def edit(self, content=None, view=None):
        self.content = content
        self.view = view


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, view=None, file=None):
        m = Msg(self, content, view)
        self.sent.append(m)
        return m

    def typing(self):
        return Typing()


@pytest.fixture
def bot(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text(FAKE.replace("PYTHON", sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cfg = config.Config(claude_bin=str(fake), default_cwd=tmp_path, settings_file=tmp_path / "s.json")
    cfg.accounts = [config.Account("one", tmp_path / "a"), config.Account("two", tmp_path / "b")]
    return Seele(cfg, config.State(tmp_path / "state.json")), fake


def calls(fake):
    return [json.loads(x) for x in (fake.parent / "claude.log").read_text().splitlines()]


def test_prompt_then_resume(bot):
    b, fake = bot
    ch = Channel()

    async def go():
        b.submit(1, ch, "hello")
        assert 1 in b.jobs  # busy before the task even starts
        while b.jobs:
            await asyncio.sleep(0.05)
        b.submit(1, ch, "again")
        while b.jobs:
            await asyncio.sleep(0.05)
    asyncio.run(go())
    status, reply = ch.sent[0], ch.sent[1]
    assert status.content.startswith("✅ **Done**") and "1 tool" in status.content and "25%" in status.content
    assert status.view is None
    assert reply.content == "answer: hello"
    first, second = calls(fake)
    assert "--resume" not in first["argv"] and first["prompt"] == "hello"
    assert second["argv"][second["argv"].index("--resume") + 1] == "sess-1"
    assert b.state.get(1).session_id == "sess-1"


def test_limit_offers_other_account(bot):
    b, _ = bot
    ch = Channel()

    async def go():
        b.submit(2, ch, "LIMIT please")
        while b.jobs:
            await asyncio.sleep(0.05)
    asyncio.run(go())
    assert ch.sent[0].content.startswith("⚠️ **Failed**")
    reply = ch.sent[1]
    assert "hit your limit" in reply.content
    assert [i.label for i in reply.view.children] == ["Retry on two"]


def test_missing_folder_frees_the_chat(bot, tmp_path):
    b, _ = bot
    b.state.get(3).cwd = str(tmp_path / "nope")
    ch = Channel()

    async def go():
        b.submit(3, ch, "hi")
        while b.jobs:
            await asyncio.sleep(0.05)
    asyncio.run(go())
    assert "doesn't exist" in ch.sent[0].content
