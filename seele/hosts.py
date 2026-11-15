"""Machines that run Claude Code for SEELE: the bot's own machine, or another one over SSH.

A remote host is reached with plain `ssh` (key login, BatchMode); a host with a `mac` is woken
with a Wake-on-LAN packet when it doesn't answer. Paths on a remote host are POSIX strings.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import posixpath
import re
import shlex
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from . import hostops

# Runs on the remote host (bash): take over the user's systemd session environment (display,
# D-Bus, ... like a local user service would have), cd, report our pid (= the process group,
# since sshd starts each session with setsid), then become `env ARGS... claude ...`.
WRAPPER = r"""
eval "$(systemctl --user show-environment 2>/dev/null | sed -e '/^[A-Za-z_][A-Za-z0-9_]*=/!d' -e 's/^/export /')"
export PATH="$HOME/.local/bin:$PATH"
cd "$1" || { echo "seele: no folder $1 on $(uname -n)" >&2; exit 3; }
shift
printf 'SEELE_PID %s\n' "$$" >&2
exec env "$@"
"""


def _windows_ssh(name: str) -> str:
    path = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / name
    return str(path) if path.exists() else name


GIT_SSH = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin" / "ssh.exe"


def default_ssh() -> str:
    """On Windows, Git for Windows' ssh: the system's OpenSSH (9.5) never passes a pipe's data or
    EOF on to the remote command, which a run's stdin needs. Git's ssh may not follow an `Include`
    with a Windows path in ~/.ssh/config: point [ssh] options at the file (-F PATH) then."""
    if os.name != "nt":
        return "ssh"
    return str(GIT_SSH) if GIT_SSH.exists() else _windows_ssh("ssh.exe")


def magic_packet(mac: str) -> bytes:
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac)
    if len(digits) != 12:
        raise ValueError(f"bad MAC address {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(digits) * 16


class HostError(Exception):
    """A host that can't be reached, or a remote look-up that failed."""


@dataclass
class Host:
    name: str
    ssh: str = ""  # ssh destination (an alias from ~/.ssh/config); empty = this machine
    mac: str = ""  # for Wake-on-LAN
    broadcast: str = "255.255.255.255"
    home: str = ""  # home folder on the host (for ~); asked over SSH when empty
    claude_bin: str = ""  # empty = [claude] binary
    default_cwd: str = "~"
    projects_root: str = "~/Projects"
    wake_timeout: float = 240.0  # seconds to wait for a woken host to answer
    ssh_bin: str = ""
    ssh_opts: tuple = ()  # extra ssh arguments, e.g. ("-F", "path/to/ssh_config")
    _up_at: float = 0.0  # when it last answered

    @property
    def local(self) -> bool:
        return not self.ssh

    # ---------- paths ----------
    def path(self, text: str) -> str:
        """`text` as an absolute path on this host (~ expanded, relative = under projects_root)."""
        text = str(text).strip()
        if self.local:
            p = Path(os.path.expandvars(text or self.default_cwd)).expanduser()
            if not p.is_absolute():
                p = Path(os.path.expandvars(self.projects_root)).expanduser() / p
            return str(p)
        text = text or self.default_cwd
        if text == "~" or text.startswith("~/"):
            text = (self.home or "~") + text[1:]
        if not text.startswith("/") and not text.startswith("~"):
            text = posixpath.join(self.path(self.projects_root), text)
        return posixpath.normpath(text) if not text.startswith("~") else text

    def short(self, path: str) -> str:
        home = (str(Path.home()) if self.local else self.home).rstrip("/\\")
        if home and (path == home or path[len(home):len(home) + 1] in ("/", "\\") and path.startswith(home)):
            return "~" + path[len(home):]
        return path

    def contains(self, folder: str, path: str) -> bool:
        """Is `path` the folder `folder` or inside it (both from `path()`)?"""
        if self.local:
            try:
                p, f = Path(path).resolve(), Path(folder).resolve()
            except OSError:
                return False
            return p == f or f in p.parents
        return path == folder or path.startswith(folder.rstrip("/") + "/")

    # ---------- commands ----------
    def ssh_argv(self, command: str, *, connect_timeout: int = 8) -> list[str]:
        return [self.ssh_bin or default_ssh(), "-T", *self.ssh_opts, "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={connect_timeout}", "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=4", self.ssh, command]

    def command(self, argv: list[str], *, cwd: str, env: dict[str, str] | None = None,
                unset: tuple[str, ...] = ()) -> tuple[list[str], dict[str, str], str | None]:
        """(local argv, local env, local cwd) that run `argv` on this host in `cwd`."""
        if self.local:
            full = {k: v for k, v in {**os.environ, **(env or {})}.items() if k not in unset}
            return argv, full, cwd
        args = [cwd]
        for name in unset:
            args += ["-u", name]
        args += [f"{k}={v}" for k, v in (env or {}).items()] + argv
        script = WRAPPER.replace("\r", "")  # a Windows checkout may have CRLF line ends
        remote = "exec bash -c " + shlex.quote(script) + " seele " + " ".join(shlex.quote(a) for a in args)
        return self.ssh_argv(remote), dict(os.environ), None

    async def _ssh(self, command: str, *, stdin: bytes | None = None, timeout: float = 20,
                   connect_timeout: int = 8) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *self.ssh_argv(command, connect_timeout=connect_timeout),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            proc.kill()
            await proc.wait()
            raise HostError(f"{self.name} didn't answer within {timeout:g} s") from None
        return proc.returncode, out, err

    async def op(self, name: str, *args: str, timeout: float = 20):
        """One `hostops` look-up on this host."""
        if self.local:
            return await asyncio.to_thread(hostops.run, name, *args)
        source = inspect.getsource(hostops).encode()
        cmd = "python3 - " + " ".join(shlex.quote(str(a)) for a in (name, *args))
        code, out, err = await self._ssh(cmd, stdin=source, timeout=timeout)
        if code == 255:
            raise HostError(f"can't reach {self.name} over SSH: {err.decode(errors='replace').strip()[-200:]}")
        try:
            answer = json.loads(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise HostError(f"{self.name}: {(err or out).decode(errors='replace').strip()[-300:] or 'no answer'}") from None
        if "error" in answer:
            raise HostError(f"{self.name}: {answer['error']}")
        self._up_at = time.monotonic()
        return answer.get("ok")

    async def fill_home(self):
        if not self.local and not self.home:
            self.home = str(await self.op("home"))

    async def put(self, data: bytes, path: str, timeout: float = 120):
        """Write `data` to `path` on this host (folders made as needed)."""
        if self.local:
            target = Path(path)
            await asyncio.to_thread(lambda: (target.parent.mkdir(parents=True, exist_ok=True), target.write_bytes(data)))
            return
        q = shlex.quote(path)
        code, _out, err = await self._ssh(f"mkdir -p \"$(dirname {q})\" && cat > {q}", stdin=data, timeout=timeout)
        if code != 0:
            raise HostError(f"couldn't write {path} on {self.name}: {err.decode(errors='replace').strip()[-200:]}")

    async def fetch(self, path: str, timeout: float = 120) -> bytes:
        if self.local:
            return await asyncio.to_thread(Path(path).read_bytes)
        code, out, err = await self._ssh(f"cat {shlex.quote(path)}", timeout=timeout)
        if code != 0:
            raise HostError(f"couldn't read {path} on {self.name}: {err.decode(errors='replace').strip()[-200:]}")
        return out

    async def kill_group(self, pid: int):
        """Stop a remote run: its whole process group (see WRAPPER)."""
        try:
            await self._ssh(f"kill -TERM -- -{int(pid)} 2>/dev/null || kill -TERM {int(pid)}", timeout=20)
        except (HostError, OSError):
            pass

    # ---------- reachability ----------
    async def reachable(self, timeout: int = 6) -> bool:
        if self.local:
            return True
        if time.monotonic() - self._up_at < 20:
            return True
        try:
            code, _out, _err = await self._ssh("true", timeout=timeout + 6, connect_timeout=timeout)
        except (HostError, OSError):
            return False
        if code == 0:
            self._up_at = time.monotonic()
        return code == 0

    def send_wake(self):
        packet = magic_packet(self.mac)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for port in (9, 7):
                s.sendto(packet, (self.broadcast, port))

    async def ensure_up(self, on_wake=None) -> None:
        """Return once the host answers over SSH, waking it with Wake-on-LAN if it can.
        `on_wake()` (async) is awaited once if a wake-up starts. Raises HostError otherwise."""
        if await self.reachable():
            await self.fill_home()
            return
        if not self.mac:
            raise HostError(f"**{self.name}** doesn't answer over SSH (asleep or off?) and has no `mac` "
                            "for Wake-on-LAN. Wake it, then send the message again.")
        try:
            self.send_wake()
        except (OSError, ValueError) as e:
            raise HostError(f"couldn't send the Wake-on-LAN packet to {self.name}: {e}") from None
        if on_wake:
            await on_wake()
        start = time.monotonic()
        last_packet = start
        while time.monotonic() - start < self.wake_timeout:
            await asyncio.sleep(5)
            if await self.reachable(timeout=4):
                await self.fill_home()
                return
            if time.monotonic() - last_packet > 30:
                last_packet = time.monotonic()
                try:
                    self.send_wake()
                except OSError:
                    pass
        raise HostError(f"**{self.name}** didn't wake up within {self.wake_timeout / 60:g} min "
                        "(Wake-on-LAN off, or it booted into another system?).")
