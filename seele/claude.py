"""Run the Claude Code CLI headless and turn its stream-json output into simple events."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODES = ("auto", "acceptEdits", "plan", "bypassPermissions")


@dataclass
class Event:
    kind: str  # init | tool | text | rate | result
    text: str = ""
    session_id: str = ""
    model: str = ""
    is_error: bool = False
    cost: float = 0.0
    turns: int = 0
    utilization: float | None = None  # 0..1, 5-hour window
    data: dict = field(default_factory=dict)


def build_argv(binary: str, *, session_id: str | None = None, model: str | None = None,
               effort: str | None = None, mode: str = "auto", system_prompt: str = "",
               disallowed: tuple[str, ...] = ("AskUserQuestion",)) -> list[str]:
    """Command line for one headless turn. The prompt itself goes in on stdin."""
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", mode]
    if session_id:
        argv += ["--resume", session_id]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    if disallowed:
        argv += ["--disallowed-tools", ",".join(disallowed)]
    return argv


def _home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def _clip(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def tool_summary(name: str, args: dict) -> str:
    """One short line describing a tool call, e.g. `Bash: git status`."""
    args = args or {}
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query",
                "description", "skill", "prompt", "path"):
        if args.get(key):
            value = str(args[key])
            if key in ("file_path", "notebook_path", "path"):
                value = _home(value)
            if key == "command":
                value = value.strip().splitlines()[0] if value.strip() else value
            return f"{name}: {_clip(value)}"
    return name


def parse_line(line: str) -> list[Event]:
    """Translate one stream-json line into zero or more events."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(d, dict):
        return []
    t = d.get("type")
    if t == "system" and d.get("subtype") == "init":
        return [Event("init", session_id=d.get("session_id", ""), model=d.get("model", ""),
                      data={"cwd": d.get("cwd", "")})]
    if t == "assistant":
        out = []
        if d.get("parent_tool_use_id"):  # a subagent's inner steps: too noisy
            return out
        for c in (d.get("message") or {}).get("content") or []:
            if c.get("type") == "tool_use":
                out.append(Event("tool", text=tool_summary(c.get("name", "?"), c.get("input") or {}),
                                 data={"name": c.get("name", "")}))
            elif c.get("type") == "text" and c.get("text", "").strip():
                out.append(Event("text", text=c["text"].strip()))
        return out
    if t == "rate_limit_event":
        info = d.get("rate_limit_info") or {}
        five = ((info.get("unifiedWindows") or {}).get("five_hour") or {})
        util = five.get("utilization")
        return [Event("rate", utilization=util, data=info)] if util is not None else []
    if t == "result":
        text = d.get("result")
        if not isinstance(text, str):
            text = "; ".join(d.get("errors") or []) or d.get("subtype", "")
        return [Event("result", text=text or "", session_id=d.get("session_id", ""),
                      is_error=bool(d.get("is_error")) or d.get("subtype") != "success",
                      cost=float(d.get("total_cost_usd") or 0), turns=int(d.get("num_turns") or 0),
                      data={"subtype": d.get("subtype", "")})]
    return []


LIMIT_RE = re.compile(r"(usage|rate|session|weekly)[ -]limit|hit your limit|limit reached|out of (extra )?usage",
                      re.I)


def looks_like_limit(text: str) -> bool:
    return bool(LIMIT_RE.search(text or ""))


class Run:
    """One `claude -p` process. Iterate `events()`; call `stop()` to cancel."""

    def __init__(self, argv: list[str], prompt: str, cwd: Path, env: dict[str, str]):
        self.argv, self.prompt, self.cwd, self.env = argv, prompt, cwd, env
        self.proc: asyncio.subprocess.Process | None = None
        self.stopped = False
        self.stderr = b""

    async def events(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=str(self.cwd), env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=64 * 1024 * 1024)
        self.proc.stdin.write(self.prompt.encode())
        await self.proc.stdin.drain()
        self.proc.stdin.close()
        err_task = asyncio.create_task(self._drain_stderr())
        got_result = False
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                for ev in parse_line(line.decode(errors="replace")):
                    got_result |= ev.kind == "result"
                    yield ev
        finally:
            code = await self.proc.wait()
            await err_task
        if not got_result:
            tail = self.stderr.decode(errors="replace").strip()[-1500:]
            if self.stopped:
                yield Event("result", text="Stopped.", is_error=True, data={"subtype": "stopped"})
            else:
                yield Event("result", text=tail or f"claude exited with code {code}", is_error=True,
                            data={"subtype": "crash"})

    async def _drain_stderr(self):
        while chunk := await self.proc.stderr.read(65536):
            self.stderr = (self.stderr + chunk)[-8192:]

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def project_dir(config_dir: Path, cwd: Path) -> Path:
    """Where Claude Code keeps the transcripts for a working directory."""
    return config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def recent_sessions(config_dir: Path, cwd: Path, limit: int = 15) -> list[tuple[str, str, float]]:
    """(session id, title, mtime) for the newest transcripts of `cwd`."""
    folder = project_dir(config_dir, cwd)
    files = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [(f.stem, session_title(f), f.stat().st_mtime) for f in files]


def session_title(path: Path) -> str:
    title, first = "", ""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"ai-title"' in line:
                    try:
                        title = json.loads(line).get("aiTitle") or title
                    except json.JSONDecodeError:
                        pass
                elif not first and '"user"' in line:
                    try:
                        d = json.loads(line)
                        if d.get("type") != "user" or d.get("isMeta"):
                            continue
                        content = d["message"]["content"]
                    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                        continue
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                        first = content
    except OSError:
        pass
    return _clip(title or first or "(untitled)", 90)
