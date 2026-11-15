"""Small file-system look-ups SEELE needs on the machine that runs Claude Code.

Self-contained on purpose (standard library only, no package imports): for another machine,
SEELE pipes this file's source into `python3 - OP ARGS...` over SSH and reads one JSON line back.
On this machine the same functions are called directly.
"""
import json
import os
import re
import shutil
import subprocess
import sys
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


OPS = {"home": op_home, "isdir": op_isdir, "size": op_size, "dirs": op_dirs, "read": op_read,
       "sessions": op_sessions, "pgrep": op_pgrep,
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
