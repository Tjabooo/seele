"""Run the Claude Code CLI headless and turn its stream-json output into simple events."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODES = ("auto", "acceptEdits", "plan", "bypassPermissions")


TASK_EVENTS = ("task_started", "task_progress", "task_updated", "task_notification")


@dataclass
class Event:
    # init | tool | text | rate | result | tasks (background_tasks_changed) | task_started |
    # task_progress | task_updated | task_notification
    kind: str
    text: str = ""
    session_id: str = ""
    model: str = ""
    is_error: bool = False
    cost: float = 0.0
    turns: int = 0
    utilization: float | None = None  # 0..1, 5-hour window
    agent: str = ""  # for a subagent's own steps: the tool_use id of the Agent call that started it
    data: dict = field(default_factory=dict)


def build_argv(binary: str, *, session_id: str | None = None, model: str | None = None,
               effort: str | None = None, mode: str = "auto", system_prompt: str = "",
               disallowed: tuple[str, ...] = ("AskUserQuestion",), live: bool = False) -> list[str]:
    """Command line for one headless run. The prompt goes in on stdin: plain text, or with
    `live` as stream-json user messages (stdin stays open, so more can follow)."""
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", mode]
    if live:
        argv += ["--input-format", "stream-json"]
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
    sub = d.get("subtype")
    if t == "system" and sub == "init":
        return [Event("init", session_id=d.get("session_id", ""), model=d.get("model", ""),
                      data={"cwd": d.get("cwd", "")})]
    if t == "system" and sub in TASK_EVENTS:
        return [Event(sub, data=d)]
    if t == "system" and sub == "background_tasks_changed":
        tasks = d.get("tasks")
        return [Event("tasks", data={"tasks": tasks if isinstance(tasks, list) else []})]
    if t == "assistant":
        out = []
        parent = d.get("parent_tool_use_id") or ""  # set for a subagent's own steps
        for c in (d.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                out.append(Event("tool", text=tool_summary(c.get("name", "?"), c.get("input") or {}),
                                 agent=parent, data={"name": c.get("name", ""), "input": c.get("input") or {}}))
            elif c.get("type") == "text" and c.get("text", "").strip():
                out.append(Event("text", text=c["text"].strip(), agent=parent))
        return out
    if t == "rate_limit_event":
        info = d.get("rate_limit_info") or {}
        five = ((info.get("unifiedWindows") or {}).get("five_hour") or {})
        return [Event("rate", utilization=five.get("utilization"), data=info)]
    if t == "result":
        text = d.get("result")
        if not isinstance(text, str):
            text = "; ".join(d.get("errors") or []) or d.get("subtype", "")
        return [Event("result", text=text or "", session_id=d.get("session_id", ""),
                      is_error=bool(d.get("is_error")) or d.get("subtype") != "success",
                      cost=float(d.get("total_cost_usd") or 0), turns=int(d.get("num_turns") or 0),
                      data={"subtype": d.get("subtype", ""), "origin": d.get("origin") or {}})]
    return []


def user_line(text: str) -> bytes:
    """One stream-json user message for `--input-format stream-json`."""
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n").encode()


def limit_reset(info: dict) -> float | None:
    """When a rejected rate limit lifts (epoch seconds), from a rate_limit_event's info.
    None while the account is still allowed. With several windows exhausted (5-hour and
    weekly), the latest reset wins: work can only go on once all of them have reset."""
    resets = []
    for win in (info.get("unifiedWindows") or {}).values():
        if isinstance(win, dict) and str(win.get("status", "")).startswith("rejected") and win.get("resetsAt"):
            resets.append(float(win["resetsAt"]))
    if not resets and str(info.get("status", "")).startswith("rejected") and info.get("resetsAt"):
        resets.append(float(info["resetsAt"]))
    return max(resets) if resets else None


RESET_TEXT = re.compile(r"resets\s+(?:(?:at|on)\s+)?(?:([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2}),?\s+(?:at\s+)?)?"
                        r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b(?:\s*\(([^)]+)\))?", re.I)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def reset_from_text(text: str, now: float | None = None) -> float | None:
    """The reset time in a limit message such as "You've hit your limit · resets 7pm
    (Europe/Berlin)" or "resets Nov 20, 7:30am", or the older "...|1794690600"."""
    if m := re.search(r"\|(\d{10})\b", text or ""):
        return float(m.group(1))
    m = RESET_TEXT.search(text or "")
    if not m:
        return None
    month, day, hour, minute, ampm, zone = m.groups()
    tz = None
    if zone:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(zone.strip())
        except Exception:
            tz = None
    now_dt = datetime.fromtimestamp(now if now is not None else time.time(), tz).astimezone(tz)
    hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    try:
        when = now_dt.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
        if month and month[:3].lower() in MONTHS:
            when = when.replace(month=MONTHS.index(month[:3].lower()) + 1, day=int(day))
            if when < now_dt - timedelta(days=1):
                when = when.replace(year=when.year + 1)
        elif when <= now_dt:
            when += timedelta(days=1)
    except ValueError:
        return None
    return when.timestamp()


LIMIT_RE = re.compile(r"(usage|rate|session|weekly)[ -]limit|hit your limit|limit reached|out of (extra )?usage",
                      re.I)


def looks_like_limit(text: str) -> bool:
    return bool(LIMIT_RE.search(text or ""))


class Run:
    """One `claude -p` process. Iterate `events()`; call `stop()` to cancel.

    With `live`, the prompt goes in as a stream-json user message and stdin stays open:
    `send()` writes more messages while it runs (Claude sees them at its next step, or as a new
    turn when it's idle waiting for background tasks). One process can then answer several
    times (one `result` per turn). Stdin is closed once a turn has ended, no background task
    is left and nothing was written since; then Claude Code exits on its own.
    """

    def __init__(self, argv: list[str], prompt: str, cwd: Path, env: dict[str, str], *,
                 live: bool = False, idle_grace: float = 20.0):
        self.argv, self.prompt, self.cwd, self.env = argv, prompt, cwd, env
        self.live, self.idle_grace = live, idle_grace
        self.proc: asyncio.subprocess.Process | None = None
        self.stopped = False
        self.stderr = b""
        self.input_open = False
        self.turn_active = False  # a turn is running (between init and its result)
        self.tasks: list[dict] = []  # running background tasks (last background_tasks_changed)
        self.results = 0
        self.written = False  # a message went in since the last result
        self._closer: asyncio.TimerHandle | None = None

    async def events(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=str(self.cwd), env=self.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=64 * 1024 * 1024)
        try:
            if self.live:
                self.proc.stdin.write(user_line(self.prompt))
                self.input_open = self.turn_active = True
            else:
                self.proc.stdin.write(self.prompt.encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        if not self.live:
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
                    self._track(ev)
                    yield ev
        finally:
            self.close_input()
            code = await self.proc.wait()
            await err_task
        if self.stopped:
            yield Event("result", text="Stopped.", is_error=True, data={"subtype": "stopped"})
        elif not got_result or code < 0 or code >= 128:  # no answer at all, or killed by a signal
            tail = self.stderr.decode(errors="replace").strip()[-1500:]
            yield Event("result", text=tail or f"claude exited with code {code}", is_error=True,
                        data={"subtype": "crash"})

    def _track(self, ev: Event):
        """Follow turns and background tasks, and close stdin once there is nothing left to do."""
        if ev.agent:
            return
        if ev.kind in ("init", "tool", "text"):
            self.turn_active = True
            self._cancel_closer()
        elif ev.kind == "tasks":
            self.tasks = ev.data.get("tasks") or []
            if not self.tasks and not self.turn_active and self.results and not self.written:
                # A finished task usually wakes Claude up for one more turn right away: give it
                # a moment, and close only if nothing starts.
                self._schedule_closer()
        elif ev.kind == "result":
            self.results += 1
            self.turn_active = False
            self.written = False
            if not self.tasks:
                self.close_input()

    def _schedule_closer(self):
        self._cancel_closer()
        loop = asyncio.get_running_loop()
        self._closer = loop.call_later(self.idle_grace, self._idle_close)

    def _cancel_closer(self):
        if self._closer:
            self._closer.cancel()
            self._closer = None

    def _idle_close(self):
        self._closer = None
        if not self.tasks and not self.turn_active and not self.written:
            self.close_input()

    async def send(self, text: str) -> bool:
        """Write one more user message into the running process. False if it can't take any
        more (not live, stdin already closed, or the process is gone): queue it instead."""
        if not (self.live and self.input_open and self.proc and self.proc.returncode is None):
            return False
        try:
            self.proc.stdin.write(user_line(text))
            self.written = True
            self._cancel_closer()
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            self.input_open = False
            return False
        return True

    def close_input(self):
        self._cancel_closer()
        if self.input_open or (self.proc and self.proc.stdin and not self.proc.stdin.is_closing()):
            self.input_open = False
            try:
                self.proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, RuntimeError, AttributeError):
                pass

    async def _drain_stderr(self):
        while chunk := await self.proc.stderr.read(65536):
            self.stderr = (self.stderr + chunk)[-8192:]

    def stop(self):
        self.stopped = True
        self.close_input()
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
