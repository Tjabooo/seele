import asyncio

import discord
import pytest

from seele import claude, config, lights
from seele.bot import Job, outcome_light


class Thread:
    def __init__(self, name, tid=7, fail=None):
        self.id, self.name, self.names, self.fail = tid, name, [], fail

    async def edit(self, name):
        if self.fail:
            raise self.fail
        self.name = name
        self.names.append(name)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, s):
        self.slept.append(s)
        self.now += s
        await asyncio.sleep(0)


def make(clock=None):
    clock = clock or Clock()
    return lights.Lights(is_thread=lambda ch: isinstance(ch, Thread), clock=clock, sleep=clock.sleep), clock


async def settle(lt):
    while any(not w.done() for w in lt.workers.values()):
        await asyncio.sleep(0)


def test_names():
    assert lights.named(lights.BUSY, "fix the bot") == "🟠 fix the bot"
    assert lights.named(lights.DONE, "🟠 fix the bot") == "🟢 fix the bot"
    assert lights.named(lights.HALTED, "") == "🔴 Claude"
    assert len(lights.named(lights.DONE, "x" * 200)) == 100
    assert lights.light_of("🟢 hi") == lights.DONE and lights.light_of("hi") == ""


def test_rename_and_skip_when_already_shown():
    lt, _ = make()
    th = Thread("🟠 hello")

    async def go():
        assert lt.set(th, lights.BUSY)  # already in the name (thread created with it): no edit
        await settle(lt)
        lt.set(th, lights.DONE)
        await settle(lt)
    asyncio.run(go())
    assert th.names == ["🟢 hello"]
    assert not lt.set(object(), lights.DONE)  # DMs and channels: nothing to rename


def test_rate_budget_coalesces_to_the_newest_light():
    lt, clock = make()
    th = Thread("job")

    async def go():
        lt.set(th, lights.BUSY)
        await settle(lt)
        lt.set(th, lights.DONE)
        await settle(lt)
        # two renames used: the next ones wait for the 10-minute window; only the last one is applied
        lt.set(th, lights.BUSY)
        lt.set(th, lights.HALTED)
        await settle(lt)
    asyncio.run(go())
    assert th.names == ["🟠 job", "🟢 job", "🔴 job"]
    assert clock.slept and clock.slept[0] > 590


def test_forbidden_turns_lights_off_for_that_thread():
    lt, _ = make()
    resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
    th = Thread("job", fail=discord.Forbidden(resp, "no"))

    async def go():
        lt.set(th, lights.DONE)
        await settle(lt)
    asyncio.run(go())
    assert th.id in lt.dead and not lt.set(th, lights.BUSY)


def test_outcome_light():
    conv = config.Conversation()
    job = Job(None, "p")
    assert outcome_light(None, conv) == lights.HALTED  # never ran
    assert outcome_light(job, conv) == lights.HALTED  # no result (host asleep, folder missing)
    job.last_result = claude.Event("result", text="ok")
    assert outcome_light(job, conv) == lights.DONE
    assert outcome_light(job, conv, crashed=True) == lights.HALTED
    conv.resume_kinds = ["limit"]
    conv.resume_at = 1.0
    assert outcome_light(job, conv) == lights.HALTED  # waits for an auto-resume
    conv = config.Conversation()
    job.last_result = claude.Event("result", text="Stopped.", is_error=True, data={"subtype": "stopped"})
    assert outcome_light(job, conv) == lights.HALTED
