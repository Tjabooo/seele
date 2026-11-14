"""A tiny Discord REST client for the `seele` CLI: post messages (with files) and create threads,
as the bot, without starting it."""
from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from . import __version__
from .text import split_message

API = "https://discord.com/api/v10"
MAX_FILES = 10  # Discord's limit per message
USER_AGENT = f"DiscordBot (https://github.com/Tjabooo/seele, {__version__})"


class RestError(Exception):
    pass


def multipart(payload: dict, files: list[Path]) -> tuple[bytes, str]:
    """A multipart/form-data body: payload_json plus files[n]."""
    boundary = "seele-" + uuid.uuid4().hex
    out = [(f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
            f"Content-Type: application/json\r\n\r\n").encode() + json.dumps(payload).encode() + b"\r\n"]
    for i, path in enumerate(files):
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        name = path.name.replace('"', "").replace("\r", "").replace("\n", "")
        out.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files[{i}]"; filename="{name}"\r\n'
                    f"Content-Type: {ctype}\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def explain(code: int, info: dict) -> str:
    """A readable error for a failed Discord request (never includes the token)."""
    msg = info.get("message") or ""
    if code == 401:
        return "Discord rejected the bot token (401). Check `token` in the config."
    if code == 403:
        return (f"no permission (403: {msg or 'Missing Access'}). Is the bot in that server, and allowed to "
                "see the channel, send messages and attach files there?")
    if code == 404:
        return f"not found (404: {msg or 'Unknown Channel'}). Check the channel or message id."
    if code == 413:
        return "upload too large (413): Discord refused the size of these files."
    details = info.get("errors")
    return f"Discord error {code}: {msg}" + (f" {json.dumps(details)[:500]}" if details else "")


def request(token: str, method: str, path: str, body: dict | None = None, files: list[Path] | None = None,
            *, opener=urllib.request.urlopen, retries: int = 3) -> dict:
    headers = {"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    data = None
    if files:
        data, headers["Content-Type"] = multipart(body or {}, files)
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with opener(req, timeout=120) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                info = json.loads(e.read() or b"{}")
            except (json.JSONDecodeError, OSError):
                info = {}
            if e.code == 429 and attempt < retries:  # rate limited: wait as told, then retry
                time.sleep(min(float(info.get("retry_after") or 1), 60))
                continue
            raise RestError(explain(e.code, info)) from None
        except urllib.error.URLError as e:
            raise RestError(f"can't reach Discord: {e.reason}") from None
    raise RestError("still rate limited by Discord; try again in a minute")


def check_files(files: list[str], max_mb: float) -> list[Path]:
    paths = [Path(f).expanduser() for f in files]
    if len(paths) > MAX_FILES:
        raise RestError(f"at most {MAX_FILES} files per message (got {len(paths)})")
    for path in paths:
        if not path.is_file():
            raise RestError(f"{path}: not a file")
        size = path.stat().st_size
        if size > max_mb * 1024 * 1024:
            raise RestError(f"{path.name} is {size / 1048576:.1f} MB; the limit is {max_mb:g} MB "
                            "(max_upload_mb in the config; Discord's own limit depends on the server's boosts)")
    return paths


def post(token: str, channel_id: int, text: str, files: list[str] | None = None, *,
         reply_to: int | None = None, max_mb: float = 10, opener=urllib.request.urlopen) -> list[dict]:
    """Post text (split into Discord-sized messages) and up to 10 files (on the last message).
    Returns the created messages."""
    paths = check_files(files or [], max_mb)
    if not text.strip() and not paths:
        raise RestError("nothing to post: give some text and/or --file")
    pieces = split_message(text) if text.strip() else [""]
    out = []
    for i, piece in enumerate(pieces):
        body: dict = {"content": piece, "allowed_mentions": {"parse": ["users"]}}
        if i == 0 and reply_to:
            body["message_reference"] = {"message_id": str(reply_to), "fail_if_not_exists": False}
        attach = paths if i == len(pieces) - 1 else []
        if attach:
            body["attachments"] = [{"id": j, "filename": p.name} for j, p in enumerate(attach)]
        out.append(request(token, "POST", f"/channels/{channel_id}/messages", body, attach or None,
                           opener=opener))
    return out


def create_thread(token: str, channel_id: int, name: str, *, opener=urllib.request.urlopen) -> dict:
    """A public thread in a text channel (not attached to a message)."""
    name = " ".join(name.split())[:100]
    if not name:
        raise RestError("the thread needs a name")
    return request(token, "POST", f"/channels/{channel_id}/threads",
                   {"name": name, "type": 11, "auto_archive_duration": 10080}, opener=opener)
