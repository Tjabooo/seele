"""Formatting helpers that don't need Discord (easy to test)."""
from __future__ import annotations

import re

FENCE = re.compile(r"^\s*```")


def split_message(text: str, limit: int = 1900) -> list[str]:
    """Split text into Discord-sized pieces at line breaks, keeping code fences balanced."""
    text = text.strip() or "(empty reply)"
    pieces: list[str] = []
    cur: list[str] = []
    size = 0
    fence = ""  # the opening fence line while inside a code block

    def flush():
        nonlocal cur, size
        if not cur:
            return
        body = "\n".join(cur)
        if fence:
            body += "\n```"
        pieces.append(body)
        cur = [fence] if fence else []
        size = len(fence) + 1 if fence else 0

    for line in text.split("\n"):
        while len(line) > limit - 10:  # one very long line: hard-wrap it
            if size:
                flush()
            head, line = line[: limit - 10 - size], line[limit - 10 - size:]
            cur.append(head)
            size += len(head) + 1
            flush()
        if size + len(line) + 1 > limit - 8:
            flush()
        cur.append(line)
        size += len(line) + 1
        if FENCE.match(line):
            fence = "" if fence else line.strip()
    if cur and not (fence and cur == [fence]):
        body = "\n".join(cur)
        if fence:
            body += "\n```"
        pieces.append(body)
    return pieces


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def code_safe(text: str) -> str:
    return text.replace("```", "'''")


def thread_name(prompt: str) -> str:
    name = " ".join(prompt.split())[:90] or "Claude"
    return name


def short_path(path: str, home: str) -> str:
    return "~" + path[len(home):] if path.startswith(home) else path
