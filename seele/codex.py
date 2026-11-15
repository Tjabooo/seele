"""Run one Codex turn with the signed-in CLI and translate its JSONL events."""
from __future__ import annotations

import json
from pathlib import Path

from . import claude
from .claude import Event

TOOL_LABELS = {
    "command_execution": "Shell command",
    "file_change": "File change",
    "mcp_tool_call": "Tool call",
    "web_search": "Web search",
    "collab_tool_call": "Agent tool call",
}
SANDBOX = "workspace-write"


def build_argv(binary: str, *, session_id: str, model: str, effort: str, system_prompt: str) -> list[str]:
    argv = [binary, "exec"]
    if session_id:
        argv.append("resume")
    argv += [
        "--json", "--skip-git-repo-check", "--model", model,
        "-c", f"model_reasoning_effort={json.dumps(effort)}",
        "-c", f"sandbox_mode={json.dumps(SANDBOX)}",
        "-c", "approval_policy=never",
        "-c", f"developer_instructions={json.dumps(system_prompt, ensure_ascii=False)}",
    ]
    if session_id:
        argv += ["--", session_id]
    return [*argv, "-"]


class Run(claude.Run):
    binary_name = "codex"

    def __init__(self, argv: list[str], prompt: str, cwd: Path, env: dict[str, str]):
        super().__init__(argv, prompt, cwd, env)
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
