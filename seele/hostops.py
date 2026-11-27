"""Small file-system look-ups SEELE needs on the machine that runs Claude Code.

Self-contained on purpose (standard library only, no package imports): for another machine,
SEELE pipes this file's source into `python3 - OP ARGS...` over SSH and reads one JSON line back.
On this machine the same functions are called directly.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _clip(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def project_dir(config_dir: Path, cwd: Path) -> Path:
    """Where Claude Code keeps the transcripts for a working directory."""
    return Path(config_dir) / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def recent_sessions(config_dir: Path, cwd: Path, limit: int = 15) -> list:
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


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser()


def op_home() -> str:
    return str(Path.home())


def op_isdir(path: str):
    """The folder's real path, or None if it isn't one."""
    p = _expand(path)
    return str(p.resolve()) if p.is_dir() else None


def op_size(path: str):
    """A file's size in bytes, or None if it isn't a file."""
    p = _expand(path)
    return p.stat().st_size if p.is_file() else None


def op_dirs(base: str, stem: str = "", limit: str = "25") -> list:
    """Sub-folders of `base` whose name starts with `stem` (hidden ones only if `stem` does)."""
    try:
        found = sorted(str(p) for p in _expand(base).iterdir()
                       if p.is_dir() and p.name.lower().startswith(stem.lower())
                       and (stem.startswith(".") or not p.name.startswith(".")))
    except OSError:
        return []
    return found[: int(limit)]


def op_read(path: str):
    try:
        return _expand(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def op_sessions(config_dir: str, cwd: str, limit: str = "25") -> list:
    return [list(s) for s in recent_sessions(_expand(config_dir), Path(cwd), int(limit))]


def op_realpath(path: str) -> str:
    return str(_expand(path).resolve())


def op_rename(src: str, dst: str) -> bool:
    _expand(src).replace(_expand(dst))
    return True


def op_pgrep(name: str) -> bool:
    if not shutil.which("pgrep"):
        return False
    return subprocess.run(["pgrep", "-x", name], stdout=subprocess.DEVNULL).returncode == 0


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def op_codex_models() -> list:
    """[[slug, [efforts], default effort], ...] from the Codex CLI's model cache (filled once it's signed in)."""
    try:
        data = json.loads((_codex_home() / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for m in data.get("models", []) if isinstance(data, dict) else []:
        if not isinstance(m, dict) or not m.get("slug") or m.get("visibility", "list") != "list":
            continue
        levels = [e.get("effort") for e in m.get("supported_reasoning_levels", []) if isinstance(e, dict) and e.get("effort")]
        out.append([m["slug"], levels, m.get("default_reasoning_level") or ""])
    return out


def op_codex_sessions(cwd: str, limit: str = "25") -> list:
    """Recent Codex sessions started in `cwd`: [[id, title, mtime], ...], newest first."""
    home = _codex_home()
    titles = {}
    try:
        for line in (home / "session_index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                titles[row["id"]] = row.get("thread_name") or ""
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    want = os.path.normcase(os.path.realpath(str(_expand(cwd))))
    files = sorted((home / "sessions").glob("*/*/*/rollout-*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    found = []
    for f in files[:400]:
        try:
            with f.open(encoding="utf-8") as fh:
                meta = json.loads(fh.readline()).get("payload") or {}
        except (OSError, ValueError, AttributeError):
            continue
        sid, where = meta.get("id"), meta.get("cwd") or ""
        if sid and where and os.path.normcase(os.path.realpath(where)) == want:
            found.append([sid, _clip(titles.get(sid, "")), f.stat().st_mtime])
            if len(found) >= int(limit):
                break
    return found


# ---------- screenshots ----------
SHOT_QUALITY = "85"


def _hypr_env() -> dict:
    """This user's environment plus the Wayland/Hyprland variables of the running session
    (an SSH login has none of them; the session exports them to systemd at start)."""
    env = dict(os.environ)
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key in ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "XDG_RUNTIME_DIR") and value \
                and not value.startswith("$'"):
            env.setdefault(key, value)
    runtime = Path(env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    sig = env.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not sig or not (runtime / "hypr" / sig).is_dir():  # stale after a Hyprland restart: newest live one
        live = sorted((d for d in (runtime / "hypr").glob("*") if (d / ".socket.sock").exists()),
                      key=lambda d: d.stat().st_mtime)
        if live:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = live[-1].name
    if not env.get("WAYLAND_DISPLAY") or not (runtime / env["WAYLAND_DISPLAY"]).exists():
        sockets = sorted(p.name for p in runtime.glob("wayland-*") if not p.name.endswith(".lock"))
        if sockets:
            env["WAYLAND_DISPLAY"] = sockets[0]
    return env


def _hyprctl(env: dict, what: str):
    if not shutil.which("hyprctl"):
        raise RuntimeError("no Hyprland here (hyprctl not found)")
    done = subprocess.run(["hyprctl", "-j", what], capture_output=True, env=env, timeout=10)
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise RuntimeError("Hyprland isn't running (or isn't answering): "
                           + (done.stdout or done.stderr).decode(errors="replace").strip()[:200]) from None


def _grim(env: dict, *args: str) -> str:
    done = subprocess.run(["grim", "-t", "jpeg", "-q", SHOT_QUALITY, *args, "-"], capture_output=True,
                          env=env, timeout=20)
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError("grim: " + done.stderr.decode(errors="replace").strip()[:200])
    return base64.b64encode(done.stdout).decode()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:40] or "shot"


def op_workspaces() -> list:
    """[[workspace name, label], ...] of this machine's Hyprland workspaces, in order."""
    if os.name == "nt":
        return []
    env = _hypr_env()
    shown = {m.get("activeWorkspace", {}).get("name"): m.get("name") for m in _hyprctl(env, "monitors")}
    titles = {}
    for c in _hyprctl(env, "clients"):
        ws = (c.get("workspace") or {}).get("name")
        if ws and c.get("mapped", True) and not c.get("hidden"):
            titles.setdefault(ws, []).append(c.get("title") or c.get("class") or "?")
    found = []
    for w in _hyprctl(env, "workspaces"):
        name = w.get("name") or str(w.get("id", ""))
        if not name:
            continue
        where = f"on {shown[name]}" if name in shown else "hidden"
        top = w.get("lastwindowtitle") or (titles.get(name) or ["empty"])[0]
        found.append([name, f"{name} · {where} · {top}", w.get("id", -1)])
    found.sort(key=lambda f: (f[2] < 0, f[2]))  # special workspaces (negative ids) last
    return [f[:2] for f in found]


def op_screenshot(workspace: str = "") -> list:
    """[[file name, base64 JPEG], ...]: every monitor, or one workspace. A workspace on a monitor is
    shot as that monitor; a hidden one as each of its windows (Hyprland renders them off screen)."""
    if os.name == "nt":
        return _windows_screenshot()
    if not shutil.which("grim"):
        raise RuntimeError("grim isn't installed")
    env = _hypr_env()
    monitors = [m for m in _hyprctl(env, "monitors") if not m.get("disabled")]
    if not workspace:
        return [[f"{_slug(m['name'])}-ws{_slug(m.get('activeWorkspace', {}).get('name', ''))}.jpg",
                 _grim(env, "-o", m["name"])] for m in monitors]
    known = {(w.get("name") or str(w.get("id"))): w for w in _hyprctl(env, "workspaces")}
    ws = known.get(workspace) or next((w for w in known.values() if str(w.get("id")) == workspace), None)
    if ws is None:
        raise RuntimeError(f"no workspace {workspace} (it exists only while it has windows)")
    name = ws.get("name") or str(ws.get("id"))
    for m in monitors:
        if m.get("activeWorkspace", {}).get("name") == name or (m.get("specialWorkspace") or {}).get("name") == name:
            return [[f"ws{_slug(name)}-{_slug(m['name'])}.jpg", _grim(env, "-o", m["name"])]]
    windows = [c for c in _hyprctl(env, "clients")
               if (c.get("workspace") or {}).get("name") == name and c.get("mapped", True) and not c.get("hidden")]
    if not windows:
        raise RuntimeError(f"workspace {name} has no windows to show")
    shots = []
    for c in windows[:10]:
        ident = str(c.get("stableId") or "")
        if not ident:
            raise RuntimeError("this Hyprland is too old to capture windows off screen")
        shots.append([f"ws{_slug(name)}-{_slug(c.get('class') or 'window')}.jpg", _grim(env, "-T", ident)])
    return shots


def _windows_screenshot() -> list:
    """The console desktop of a Windows machine. SEELE runs without a desktop (a background task),
    so ffmpeg's GDI capture runs once as an interactive task in the signed-in user's session."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg isn't installed (winget install ffmpeg)")
    folder = Path(tempfile.mkdtemp(prefix="seele-shot-"))
    out, cmd, task = folder / "desktop.jpg", folder / "shot.cmd", f"SEELE-screenshot-{os.getpid()}"
    cmd.write_text(f'@"{ffmpeg}" -loglevel error -f gdigrab -i desktop -frames:v 1 -q:v 3 -y "{out}" '
                   f'> "{folder / "log.txt"}" 2>&1\r\n', encoding="ascii")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        made = subprocess.run(["schtasks", "/create", "/tn", task, "/tr", f'conhost.exe --headless "{cmd}"',
                               "/sc", "once", "/st", "00:00", "/it", "/f"],
                              capture_output=True, timeout=20, creationflags=flags)
        if made.returncode != 0:
            raise RuntimeError("schtasks: " + (made.stderr or made.stdout).decode(errors="replace").strip()[:200])
        subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True, timeout=20, creationflags=flags)
        size, deadline = -1, time.monotonic() + 15
        while time.monotonic() < deadline:  # done once the file stops growing
            time.sleep(0.5)
            now = out.stat().st_size if out.exists() else -1
            if now > 0 and now == size:
                return [["desktop.jpg", base64.b64encode(out.read_bytes()).decode()]]
            size = now
        log = (folder / "log.txt").read_text(errors="replace").strip() if (folder / "log.txt").exists() else ""
        raise RuntimeError("no picture from the desktop (nobody signed in, or the screen is locked?) "
                           + log[:200])
    finally:
        subprocess.run(["schtasks", "/delete", "/tn", task, "/f"], capture_output=True, timeout=20,
                       creationflags=flags)
        shutil.rmtree(folder, ignore_errors=True)


OPS = {"home": op_home, "codex_models": op_codex_models, "codex_sessions": op_codex_sessions, "isdir": op_isdir, "size": op_size, "dirs": op_dirs, "read": op_read,
       "sessions": op_sessions, "pgrep": op_pgrep,
       "workspaces": op_workspaces, "screenshot": op_screenshot,
       "realpath": op_realpath, "rename": op_rename}


def run(op: str, *args: str):
    if op not in OPS:
        raise ValueError(f"unknown host op {op!r}")
    return OPS[op](*args)


def main(argv: list) -> int:
    try:
        print(json.dumps({"ok": run(*argv)}))
    except Exception as e:  # noqa: BLE001 - the error goes back to SEELE as text
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
