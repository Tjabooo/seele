"""Plan usage (the numbers behind Claude Code's /usage) for each configured account."""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .claude import reset_from_text

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
LABELS = {
    "session": "Session (5 h)",
    "weekly_all": "Week, all models",
    "weekly_scoped": "Week",
}


def read_token(config_dir: Path) -> str | None:
    """The account's OAuth access token, or None if missing or expired."""
    try:
        oauth = json.loads((config_dir / ".credentials.json").read_text())["claudeAiOauth"]
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    if oauth.get("expiresAt", 0) / 1000 < time.time() + 60:
        return None
    return oauth.get("accessToken")


def fetch(token: str, timeout: float = 10) -> dict:
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "seele",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def bar(percent: float, width: int = 12) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def until(iso: str | None, now: datetime | None = None) -> str:
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = now or datetime.now(timezone.utc)
    mins = max(0, int((when - now).total_seconds() // 60))
    if mins >= 48 * 60:
        return f"resets in {mins // 1440} d {mins % 1440 // 60} h"
    if mins >= 60:
        return f"resets in {mins // 60} h {mins % 60:02d} min"
    return f"resets in {mins} min"


def rows(data: dict) -> list[tuple[str, float, str]]:
    """(label, percent, resets_at) rows from the usage response."""
    out = []
    limits = data.get("limits")
    if isinstance(limits, list) and limits:
        for lim in limits:
            label = LABELS.get(lim.get("kind"), str(lim.get("kind", "?")).replace("_", " "))
            if lim.get("kind") == "weekly_scoped" and lim.get("scope"):
                label = f"Week, {lim['scope']}"
            if lim.get("kind") == "weekly_scoped" and not lim.get("is_active") and not lim.get("percent"):
                continue
            out.append((label, float(lim.get("percent") or 0), lim.get("resets_at") or ""))
        return out
    for key, label in (("five_hour", "Session (5 h)"), ("seven_day", "Week, all models"),
                       ("seven_day_opus", "Week, Opus"), ("seven_day_sonnet", "Week, Sonnet")):
        win = data.get(key)
        if isinstance(win, dict) and win.get("utilization") is not None:
            out.append((label, float(win["utilization"]), win.get("resets_at") or ""))
    return out


def format_rows(data: dict) -> str:
    lines = []
    for label, pct, resets in rows(data):
        lines.append(f"`{bar(pct)}` **{pct:.0f}%** {label} · {until(resets)}".rstrip(" ·"))
    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("used_credits") is not None:
        lines.append(f"Extra usage: {extra['used_credits']} of {extra.get('monthly_limit')}")
    return "\n".join(lines) or "No limits reported."


def _window(utilization: float, resets_at: str | None) -> dict:
    epoch = None
    if resets_at:
        try:
            epoch = int(datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return {"utilization": round(utilization, 4), "resets_at": resets_at or None, "resets_epoch": epoch}


def windows(data: dict) -> dict:
    """The five-hour and weekly (all models) windows from the usage API, utilization as 0..1."""
    out = {}
    for key in ("five_hour", "seven_day"):
        win = data.get(key)
        if isinstance(win, dict) and win.get("utilization") is not None:
            out[key] = _window(float(win["utilization"]) / 100, win.get("resets_at"))
    for lim in data.get("limits") or []:
        key = {"session": "five_hour", "weekly_all": "seven_day"}.get(lim.get("kind")) if isinstance(lim, dict) else None
        if key and key not in out and lim.get("percent") is not None:
            out[key] = _window(float(lim["percent"]) / 100, lim.get("resets_at"))
    return out


TEXT_LINE = re.compile(r"Current (session|week \(all models\)):\s*(\d+(?:\.\d+)?)%\s*used(.*)", re.I)


def windows_from_text(text: str, now: float | None = None) -> dict:
    """The same windows from `claude -p /usage` output ("Current session: 6% used · resets ...")."""
    out = {}
    for line in text.splitlines():
        m = TEXT_LINE.search(line)
        if not m:
            continue
        key = "five_hour" if m.group(1).lower() == "session" else "seven_day"
        reset = reset_from_text(m.group(3), now)
        iso = datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset else None
        out[key] = _window(float(m.group(2)) / 100, iso)
    return out


async def cli_usage(binary: str, config_dir: Path, env: dict[str, str]) -> str:
    """`claude -p /usage` output; it also refreshes an expired login."""
    proc = await asyncio.create_subprocess_exec(
        binary, "-p", "/usage", env={**env, "CLAUDE_CONFIG_DIR": str(config_dir)},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 90)
    except TimeoutError:
        proc.kill()
        raise
    return out.decode(errors="replace")


async def account_windows(binary: str, config_dir: Path, env: dict[str, str]) -> dict:
    """{"five_hour": {...}, "seven_day": {...}, "source": "api" | "cli"} for one account, or
    {"error": "..."} when neither the API nor the CLI gave numbers."""
    token = read_token(config_dir)
    if token:
        try:
            found = windows(await asyncio.to_thread(fetch, token))
            if found:
                return {**found, "source": "api"}
        except Exception:
            pass
    try:
        text = await cli_usage(binary, config_dir, env)
    except (TimeoutError, OSError) as e:
        return {"error": f"claude -p /usage failed: {e or 'timed out'}"}
    found = windows_from_text(text)
    return {**found, "source": "cli"} if found else {"error": text.strip()[:300] or "no usage reported"}


async def for_account(binary: str, config_dir: Path, env: dict[str, str]) -> str:
    """Usage text for one account. Falls back to `claude -p /usage`, which refreshes the login."""
    token = read_token(config_dir)
    if token:
        try:
            data = await asyncio.to_thread(fetch, token)
            return format_rows(data)
        except Exception:
            pass
    try:
        text = await cli_usage(binary, config_dir, env)
    except TimeoutError:
        return "Timed out asking Claude Code for usage."
    keep = [ln for ln in text.splitlines() if "% used" in ln or "resets" in ln]
    return "\n".join(keep) or text.strip()[:900]
