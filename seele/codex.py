"""Run one Codex turn with the signed-in CLI and translate its JSONL events."""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path

from . import __version__, claude
from .claude import Event

TOOL_LABELS = {
    "command_execution": "Shell command",
    "file_change": "File change",
    "mcp_tool_call": "Tool call",
    "web_search": "Web search",
    "collab_tool_call": "Agent tool call",
}
SANDBOX = "workspace-write"
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")  # what Codex models take (per-model subsets)
MODEL_HINTS = ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-astra", "gpt-6-luna"]


def build_argv(binary: str, *, session_id: str, model: str, effort: str, system_prompt: str,
               sandbox: str = SANDBOX) -> list[str]:
    argv = [binary, "exec"]
    if session_id:
        argv.append("resume")
    argv += ["--json", "--skip-git-repo-check"]
    if model:  # empty: the CLI's own default model
        argv += ["--model", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={json.dumps(effort)}"]
    argv += [
        "-c", f"sandbox_mode={json.dumps(sandbox)}",
        "-c", "approval_policy=never",
        "-c", f"developer_instructions={json.dumps(system_prompt, ensure_ascii=False)}",
    ]
    if session_id:
        argv += ["--", session_id]
    return [*argv, "-"]


async def read_rate_limits(argv: list[str], env: dict[str, str], cwd: str | None, *, timeout: float = 15) -> dict:
    """Read account limits from the CLI's stdio app server without starting a turn."""
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    def send(message: dict):
        proc.stdin.write((json.dumps(message) + "\n").encode())

    async def receive(reply_id: int) -> dict:
        while line := await proc.stdout.readline():
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            if isinstance(answer, dict) and answer.get("id") == reply_id:
                result = answer.get("result")
                if isinstance(result, dict):
                    return result
                break
        raise RuntimeError("Codex app server did not return a result")

    async def read() -> dict:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "seele", "version": __version__}}})
        await proc.stdin.drain()
        await receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        await proc.stdin.drain()
        return await receive(2)

    try:
        return await asyncio.wait_for(read(), timeout)
    finally:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.communicate()


class Run(claude.Run):
    binary_name = "codex"

    def __init__(self, argv: list[str], prompt: str, cwd: Path | None, env: dict[str, str], *,
                 live: bool = False, remote_stop=None):
        super().__init__(argv, prompt, cwd, env, live=False, remote_stop=remote_stop)
        self.reply = ""
        self.error = ""

    def parse(self, line: str) -> list[Event]:
        try:
            data = json.loads(line)
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        kind = data.get("type")
        if kind == "thread.started":
            return [Event("init", session_id=data["thread_id"])]
        if kind == "turn.completed":
            return [Event("result", text=self.reply or "Codex completed without a text reply.", turns=1)]
        if kind == "turn.failed":
            error = data.get("error") or {}
            return [Event("result", text=error.get("message") or self.error or "Codex turn failed.",
                          is_error=True)]
        if kind == "error":
            self.error = data.get("message") or ""
        item = data.get("item") or {}
        item_type = item.get("type")
        if kind == "item.completed" and item_type == "agent_message":
            self.reply = item["text"]
            return [Event("text", text=self.reply)]
        if kind == "item.started" and item_type in TOOL_LABELS:
            return [Event("tool", text=TOOL_LABELS[item_type])]
        return []

    def crash(self, code: int) -> Event:
        event = super().crash(code)
        if b"no rollout found for thread id" in self.stderr:
            event.data["subtype"] = "missing_session"
        elif self.error:
            event.text = self.error
        return event
