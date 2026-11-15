"""Is the Claude API reachable again? Watched after a network outage or API server errors, so the
stopped work resumes by itself once it is."""
from __future__ import annotations

import asyncio
import logging
import socket
import time

import aiohttp

log = logging.getLogger("seele.net")
API_HOST = "api.anthropic.com"


async def api_reachable(host: str = API_HOST, timeout: float = 8.0) -> bool:
    """The API's name resolves and an HTTPS HEAD without credentials gets an answer below 500."""
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM), timeout)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=True) as session:
            async with session.head(f"https://{host}/", allow_redirects=False) as resp:
                return resp.status < 500
    except Exception as e:  # DNS (EAI_AGAIN), refused, timed out, TLS, ...: not reachable
        log.debug("API check: %s", e)
        return False


class ApiWatch:
    """Counts good checks in a row: the API is back after `need` of them, and work may resume
    `settle` seconds after that. Any failure (a check, or a fresh transient failure reported by
    `reset()`) starts the count over."""

    def __init__(self, probe=api_reachable, need: int = 2, settle: float = 30.0):
        self.probe, self.need, self.settle = probe, need, settle
        self.generation = 0
        self.reset()

    def reset(self):
        self.good, self.first_ok, self.back_at = 0, 0.0, 0.0
        self.generation += 1  # a check that was already under way doesn't count

    async def check(self, now: float | None = None) -> bool:
        generation = self.generation
        ok = await self.probe()
        if generation != self.generation:
            return ok
        now = time.time() if now is None else now
        if not ok:
            if self.good:
                log.info("API check failed: waiting for it again")
            self.reset()
            return False
        self.good += 1
        if self.good == 1:
            self.first_ok = now
        if self.good >= self.need and not self.back_at:
            self.back_at = now
            log.info("API reachable again (%d good checks in a row)", self.good)
        return True

    def ready(self, now: float | None = None) -> bool:
        """Back, and settled for long enough to resume."""
        now = time.time() if now is None else now
        return bool(self.back_at) and now >= self.back_at + self.settle
