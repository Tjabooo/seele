"""Hosts: paths, the SSH wrapper, host look-ups, Wake-on-LAN, per-conversation machines."""
import asyncio
import inspect
import json
import shutil
import subprocess
import sys
import time

import pytest

from seele import claude, config, hostops, hosts
from seele.bot import Seele
from seele.hosts import Host


def remote(**kw):
    return Host("magi", ssh="arch-desktop", home="/home/me", **kw)


def test_remote_paths():
    h = remote()
    assert h.path("~") == "/home/me"
    assert h.path("") == "/home/me"
    assert h.path("seele") == "/home/me/Projects/seele"
    assert h.path("~/Projects/x/../y") == "/home/me/Projects/y"
    assert h.path("/srv/data") == "/srv/data"
    assert h.short("/home/me/Projects/y") == "~/Projects/y"
    assert h.short("/home/meow") == "/home/meow"
    assert h.contains("/home/me/Projects/y", "/home/me/Projects/y/sub")
    assert not h.contains("/home/me/Projects/y", "/home/me/Projects/yz")


def test_local_paths(tmp_path):
    h = Host("here", default_cwd=str(tmp_path), projects_root=str(tmp_path / "p"))
    assert h.local
    assert h.path("") == str(tmp_path)
    assert h.path("proj") == str(tmp_path / "p" / "proj")


def test_remote_command_is_ssh_with_the_wrapper():
    argv, env, cwd = remote().command(["claude", "-p", "it's"], cwd="/home/me/x y",
                                      env={"A": "1"}, unset=("CLAUDE_CONFIG_DIR",))
    assert argv[0].endswith("ssh") or argv[0].endswith("ssh.exe")
    assert "BatchMode=yes" in argv and argv[-2] == "arch-desktop" and cwd is None
    assert argv[-1].startswith("exec bash -c ")
    assert "'/home/me/x y'" in argv[-1] and "A=1" in argv[-1] and "-u CLAUDE_CONFIG_DIR" in argv[-1]


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_wrapper_cds_sets_env_and_reports_its_pid(tmp_path):
    """Run the remote wrapper locally: the part after `ssh host` is what the host executes."""
    out = subprocess.run(["bash", "-c", hosts.WRAPPER, "seele", str(tmp_path), "-u", "GONE", "A=1",
                          sys.executable, "-c", "import os,json;print(json.dumps([os.getcwd(),os.environ.get('A'),"
                          "os.environ.get('GONE'),os.getpid()]))"],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "GONE": "x", "HOME": str(tmp_path)})
    cwd, a, gone, pid = json.loads(out.stdout)
    assert (cwd, a, gone) == (str(tmp_path), "1", None)
    assert f"SEELE_PID {pid}" in out.stderr  # exec kept the pid, so killing it hits claude itself


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_wrapper_reports_a_missing_folder(tmp_path):
    out = subprocess.run(["bash", "-c", hosts.WRAPPER, "seele", str(tmp_path / "nope"), "true"],
                         capture_output=True, text=True)
    assert out.returncode == 3 and "no folder" in out.stderr


def test_hostops_source_runs_standalone(tmp_path):
    """What a remote host runs: hostops' source on stdin of `python3 - OP ARGS`."""
    (tmp_path / "Alpha").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "f.txt").write_text("hello")
    src = inspect.getsource(hostops)

    def op(*args):
        out = subprocess.run([sys.executable, "-I", "-", *args], input=src, capture_output=True, text=True, cwd="/")
        return json.loads(out.stdout)

    assert op("isdir", str(tmp_path / "Alpha")) == {"ok": str((tmp_path / "Alpha").resolve())}
    assert op("isdir", str(tmp_path / "f.txt")) == {"ok": None}
    assert op("size", str(tmp_path / "f.txt")) == {"ok": 5}
    assert op("dirs", str(tmp_path), "a") == {"ok": [str(tmp_path / "Alpha")]}
    assert op("read", str(tmp_path / "f.txt")) == {"ok": "hello"}
    assert "error" in op("nope")


def test_hostops_sessions_match_claude(tmp_path):
    cwd = tmp_path / "proj"
    folder = claude.project_dir(tmp_path, cwd)
    folder.mkdir(parents=True)
    (folder / "s1.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "hi there"}}) + "\n")
    assert hostops.op_sessions(str(tmp_path), str(cwd)) == [list(s) for s in claude.recent_sessions(tmp_path, cwd)]
    assert hostops.op_sessions(str(tmp_path), str(cwd))[0][:2] == ["s1", "hi there"]


def test_magic_packet():
    p = hosts.magic_packet("30:c5:99:a8:69:23")
    assert len(p) == 102 and p[:6] == b"\xff" * 6 and p[6:12] == bytes.fromhex("30c599a86923")
    with pytest.raises(ValueError):
        hosts.magic_packet("nope")


def test_ensure_up_wakes_then_waits(monkeypatch):
    h = remote(mac="30:c5:99:a8:69:23", wake_timeout=30)
    answers = iter([False, False, True])
    sent, woke = [], []

    async def reachable(timeout=6):
        return next(answers)

    async def fast_sleep(_s):
        pass

    async def on_wake():
        woke.append(1)
    monkeypatch.setattr(h, "reachable", reachable)
    monkeypatch.setattr(h, "send_wake", lambda: sent.append(1))
    monkeypatch.setattr(hosts.asyncio, "sleep", fast_sleep)
    asyncio.run(h.ensure_up(on_wake=on_wake))
    assert sent and woke == [1]


def test_ensure_up_without_mac_says_so(monkeypatch):
    h = remote()

    async def reachable(timeout=6):
        return False
    monkeypatch.setattr(h, "reachable", reachable)
    with pytest.raises(hosts.HostError, match="Wake-on-LAN"):
        asyncio.run(h.ensure_up())


def test_config_hosts_and_accounts(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(f'''
host_name = "geofront"
default_host = "magi"
[claude]
default_cwd = "{tmp_path}"
[hosts.magi]
ssh = "arch-desktop"
mac = "30:c5:99:a8:69:23"
home = "/home/me"
[hosts.unit]
ssh = "arch-laptop"
home = "/home/me"
[accounts.claude]
config_dir = "~/.claude"
[accounts.claude2]
config_dir = "~/.claude-2"
paths = ["~/Projects/helix-hollow"]
[bot]
ssh = "home-server"
command = "seele.exe"
[ssh]
options = ["-F", "C:/x/ssh_config"]
''')
    cfg = config.load(path)
    assert list(cfg.hosts) == ["geofront", "magi", "unit"] and cfg.host().name == "magi"
    assert cfg.host("geofront").local and cfg.host("nope").name == "magi"
    magi = cfg.host("magi")
    assert cfg.account_for("/home/me/Projects/helix-hollow/src", None, magi).name == "claude2"
    assert cfg.account_for("/home/me/Projects/other", None, magi).name == "claude"
    c2 = cfg.accounts[1]
    assert config.host_env(cfg, c2, magi)[0]["CLAUDE_CONFIG_DIR"] == "/home/me/.claude-2"
    assert config.host_env(cfg, cfg.accounts[0], magi)[1] == ("CLAUDE_CONFIG_DIR",)
    assert (cfg.bot_ssh, cfg.bot_command) == ("home-server", "seele.exe")
    assert magi.ssh_argv("true")[2:4] == ["-F", "C:/x/ssh_config"]


def test_config_rejects_unknown_default_host(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('default_host = "nope"\n')
    with pytest.raises(ValueError):
        config.load(path)


def test_run_stops_the_remote_side(tmp_path):
    """A remote run says its pid on stderr; stop() hands it to remote_stop."""
    script = tmp_path / "fake.py"
    script.write_text("import sys,time\nprint('SEELE_PID 4242', file=sys.stderr, flush=True)\ntime.sleep(30)\n")
    stopped = []

    async def remote_stop(pid):
        stopped.append(pid)

    async def go():
        run = claude.Run([sys.executable, str(script)], "hi", tmp_path, {}, remote_stop=remote_stop)
        events = run.events()
        task = asyncio.ensure_future(events.__anext__())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if run.remote_pid:
                break
        run.stop()
        ev = await asyncio.wait_for(task, 10)
        await asyncio.sleep(0.05)
        return ev, run
    ev, run = asyncio.run(go())
    assert stopped == [4242] and ev.data.get("subtype") == "stopped"
    assert b"SEELE_PID" not in run.stderr


def test_host_switch_resets_folder_and_session(tmp_path):
    cfg = config.Config(default_cwd=tmp_path)
    cfg.accounts = [config.Account("claude", tmp_path / ".claude")]
    cfg.local_host = "here"
    cfg.hosts = {"here": Host("here", default_cwd=str(tmp_path), projects_root=str(tmp_path)),
                 "magi": remote()}
    cfg.default_host = "here"
    st = config.State(tmp_path / "state.json")
    bot = Seele(cfg, st)
    conv = st.get(5)
    conv.cwd, conv.session_id = str(tmp_path), "abc"
    msg = asyncio.run(bot.apply_conv(5, {"host": "magi"}))
    assert "host='magi'" in msg and (conv.host, conv.cwd, conv.session_id) == ("magi", "", "")
    cwd, *_ = bot.settings_for(conv)
    assert cwd == "/home/me"
    asyncio.run(bot.apply_conv(5, {"host": "default"}))
    assert conv.host == "" and bot.settings_for(conv)[0] == str(tmp_path)


def test_reachable_caches(monkeypatch):
    h = remote()
    h._up_at = time.monotonic()
    assert asyncio.run(h.reachable())
