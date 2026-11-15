"""Traffic lights on conversation threads: the thread name starts with how its latest prompt is doing.

🟠 working · 🟢 finished · 🔴 not finished and nothing running (failed, stopped, cut off by a restart,
or waiting for an auto-resume after a usage limit or an outage).

Discord lets a thread's name change only twice per 10 minutes, so each thread gets one small worker
that applies the newest light when its budget allows and skips states that were already overtaken
(a quick follow-up prompt may go straight from 🟢 to 🟢 without ever showing 🟠).
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord

log = logging.getLogger("seele.lights")

BUSY, DONE, HALTED = "🟠", "🟢", "🔴"
LIGHTS = (BUSY, DONE, HALTED)
WINDOW = 600.0  # seconds
PER_WINDOW = 2  # name changes Discord allows per thread per WINDOW
EDIT_TIMEOUT = 30.0  # discord.py sleeps through a 429 inside edit(): don't let it hold a stale light
MAX_FAILURES = 5


def strip(name: str) -> str:
    """The thread name without a light in front."""
    for light in LIGHTS:
        if name.startswith(light):
            return name[len(light):].lstrip()
    return name


def named(light: str, name: str) -> str:
    return f"{light} {strip(name) or 'Claude'}"[:100]


def light_of(name: str) -> str:
    return next((light for light in LIGHTS if name.startswith(light)), "")


def is_thread(channel) -> bool:
    return isinstance(channel, discord.Thread)


class Lights:
    def __init__(self, is_thread=is_thread, clock=time.monotonic, sleep=asyncio.sleep):
        self.is_thread, self.clock, self.sleep = is_thread, clock, sleep
        self.want: dict[int, tuple[object, str]] = {}  # thread id -> (thread, light it should show)
        self.shown: dict[int, str] = {}  # thread id -> light its name has now
        self.edits: dict[int, list[float]] = {}  # thread id -> when we renamed it (last PER_WINDOW)
        self.workers: dict[int, asyncio.Task] = {}
        self.dead: set[int] = set()  # no permission to rename

    def set(self, channel, light: str) -> bool:
        """Ask for a light on this thread. False when it isn't a thread we can rename."""
        if not self.is_thread(channel) or channel.id in self.dead:
            return False
        tid = channel.id
        self.want[tid] = (channel, light)
        if tid not in self.shown:
            self.shown[tid] = light_of(getattr(channel, "name", "") or "")
        worker = self.workers.get(tid)
        if worker is None or worker.done():
            self.workers[tid] = asyncio.create_task(self.apply(tid))
        return True

    def wait_for(self, tid: int) -> float:
        now = self.clock()
        recent = [t for t in self.edits.get(tid, []) if now - t < WINDOW]
        self.edits[tid] = recent
        if len(recent) < PER_WINDOW:
            return 0.0
        return recent[0] + WINDOW - now + 1.0

    async def apply(self, tid: int):
        failures = 0
        while True:
            channel, light = self.want[tid]
            if self.shown.get(tid) == light:
                return
            wait = self.wait_for(tid)
            if wait > 0:
                await self.sleep(wait)
                continue  # the wanted light may have changed meanwhile
            self.edits[tid].append(self.clock())
            try:
                new = await asyncio.wait_for(channel.edit(name=named(light, channel.name or "")), EDIT_TIMEOUT)
            except discord.Forbidden:
                log.warning("no permission to rename thread %s: lights off there", tid)
                self.dead.add(tid)
                return
            except (discord.HTTPException, asyncio.TimeoutError, OSError) as e:
                failures += 1
                log.warning("could not rename thread %s (%s/%s): %s", tid, failures, MAX_FAILURES, e)
                if failures >= MAX_FAILURES:
                    return
                await self.sleep(30)
                continue
            failures = 0
            self.shown[tid] = light
            if isinstance(new, discord.Thread) and self.want[tid][0] is channel:
                self.want[tid] = (new, self.want[tid][1])
