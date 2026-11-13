import json
from datetime import datetime, timezone
from pathlib import Path

from seele import claude, config, usage
from seele.__main__ import app_id_from_token, invite_links
from seele.text import duration, split_message


def line(**d):
    return json.dumps(d)


def test_build_argv_minimal_and_full():
    argv = claude.build_argv("claude")
    assert argv[:5] == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert "--resume" not in argv and "--effort" not in argv
    argv = claude.build_argv("claude", session_id="abc", model="opus", effort="xhigh", mode="plan",
                             system_prompt="hi")
    for pair in (["--resume", "abc"], ["--model", "opus"], ["--effort", "xhigh"],
                 ["--permission-mode", "plan"], ["--append-system-prompt", "hi"]):
        i = argv.index(pair[0])
        assert argv[i + 1] == pair[1]


def test_parse_init_tool_text_result():
    [ev] = claude.parse_line(line(type="system", subtype="init", session_id="s1", model="m", cwd="/x"))
    assert (ev.kind, ev.session_id, ev.model) == ("init", "s1", "m")
    evs = claude.parse_line(line(type="assistant", message={"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "git status\nmore"}},
        {"type": "text", "text": "  Looking.  "}]}))
    assert [(e.kind, e.text) for e in evs] == [("tool", "Bash: git status"), ("text", "Looking.")]
    [ev] = claude.parse_line(line(type="result", subtype="success", is_error=False, result="done",
                                  session_id="s1", num_turns=3, total_cost_usd=0.5))
    assert (ev.kind, ev.text, ev.is_error, ev.turns, ev.cost) == ("result", "done", False, 3, 0.5)


def test_parse_skips_subagent_noise_and_garbage():
    assert claude.parse_line(line(type="assistant", parent_tool_use_id="t1",
                                  message={"content": [{"type": "text", "text": "inner"}]})) == []
    assert claude.parse_line("not json") == []
    assert claude.parse_line(line(type="user", message={})) == []


def test_parse_error_result_and_rate():
    [ev] = claude.parse_line(line(type="result", subtype="error_during_execution", is_error=True,
                                  errors=["boom"]))
    assert ev.is_error and ev.text == "boom"
    [ev] = claude.parse_line(line(type="rate_limit_event", rate_limit_info={
        "unifiedWindows": {"five_hour": {"utilization": 0.42}}}))
    assert ev.kind == "rate" and ev.utilization == 0.42


def test_tool_summary_shortens_home():
    home = str(Path.home())
    assert claude.tool_summary("Read", {"file_path": f"{home}/a/b.py"}) == "Read: ~/a/b.py"
    assert claude.tool_summary("TodoWrite", {"todos": []}) == "TodoWrite"


def test_looks_like_limit():
    assert claude.looks_like_limit("You've hit your limit · resets 7pm")
    assert claude.looks_like_limit("Claude usage limit reached.")
    assert not claude.looks_like_limit("All tests passed.")


def test_split_message_keeps_fences_balanced():
    text = "intro\n```py\n" + "\n".join(f"x = {i}" for i in range(600)) + "\n```\noutro"
    pieces = split_message(text, limit=500)
    assert len(pieces) > 1
    for p in pieces:
        assert len(p) <= 500
        assert p.count("```") % 2 == 0, p
    assert pieces[0].startswith("intro")
    assert pieces[-1].endswith("outro")


def test_split_message_hard_wraps_long_lines():
    pieces = split_message("a" * 5000, limit=1000)
    assert all(len(p) <= 1000 for p in pieces)
    assert "".join(pieces) == "a" * 5000


def test_split_short_and_empty():
    assert split_message("hi") == ["hi"]
    assert split_message("  ") == ["(empty reply)"]


def test_duration():
    assert duration(5) == "0:05"
    assert duration(3725) == "1:02:05"


def test_usage_rows_from_limits_and_windows():
    data = {"limits": [
        {"kind": "session", "percent": 23, "resets_at": "2026-11-13T18:00:00+00:00", "is_active": True},
        {"kind": "weekly_all", "percent": 5, "resets_at": "2026-11-14T18:00:00+00:00"},
        {"kind": "weekly_scoped", "percent": 0, "scope": None, "is_active": False},
    ]}
    rows = usage.rows(data)
    assert [r[:2] for r in rows] == [("Session (5 h)", 23.0), ("Week, all models", 5.0)]
    old = {"five_hour": {"utilization": 12.0, "resets_at": None}, "seven_day": None}
    assert usage.rows(old) == [("Session (5 h)", 12.0, "")]
    assert "23%" in usage.format_rows(data)


def test_until_and_bar():
    now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    assert usage.until("2026-11-13T14:30:00+00:00", now) == "resets in 2 h 30 min"
    assert usage.until("2026-11-13T12:20:00Z", now) == "resets in 20 min"
    assert usage.bar(50, 10) == "█████░░░░░"


def test_read_token_respects_expiry(tmp_path):
    (tmp_path / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "tok", "expiresAt": 9_999_999_999_000}}))
    assert usage.read_token(tmp_path) == "tok"
    (tmp_path / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "tok", "expiresAt": 1000}}))
    assert usage.read_token(tmp_path) is None


def test_config_accounts_and_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    proj = tmp_path / "proj"
    proj.mkdir()
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(f'''
token = "t"
owners = [1, 2]
[people]
1 = "Ann"
[claude]
effort = "high"
[accounts.main]
config_dir = "{tmp_path}/a"
[accounts.alt]
config_dir = "{tmp_path}/b"
paths = ["{proj}"]
effort = "xhigh"
''')
    cfg = config.load(cfg_file)
    assert cfg.token == "t" and cfg.owners == {1, 2} and cfg.effort == "high"
    assert cfg.account_for(tmp_path).name == "main"
    assert cfg.account_for(proj).name == "alt"
    assert cfg.account_for(proj).effort == "xhigh"
    (proj / "sub").mkdir()
    assert cfg.account_for(proj / "sub").name == "alt"
    assert cfg.account_for(proj, "main").name == "main"
    assert cfg.name_for(1, "ann_discord") == "Ann" and cfg.name_for(2, "Bob") == "Bob"
    assert config.tag_prompt("Ann", "hi") == "[Discord message from Ann]\nhi"


def test_state_roundtrip_and_inherit(tmp_path):
    st = config.State(tmp_path / "s.json")
    conv = st.get(5)
    conv.session_id, conv.cwd, conv.effort, conv.listen = "abc", "/x", "max", True
    st.save()
    again = config.State(tmp_path / "s.json").get(5)
    assert (again.session_id, again.cwd, again.effort, again.listen) == ("abc", "/x", "max", True)
    child = again.inherit()
    assert child.session_id == "" and child.cwd == "/x" and child.effort == "max" and not child.listen


def test_set_default_effort_through_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"modelSettings": {"m1": {"effortLevel": "low"}}, "other": 1}))
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    assert config.set_default_effort(link, "xhigh") == ["m1"]
    assert link.is_symlink()
    data = json.loads(real.read_text())
    assert data["modelSettings"]["m1"]["effortLevel"] == "xhigh" and data["other"] == 1
    assert config.default_effort(link) == "xhigh"


def test_sessions_listing(tmp_path):
    cwd = Path("/home/u/my.proj")
    folder = claude.project_dir(tmp_path, cwd)
    assert folder.name == "-home-u-my-proj"
    folder.mkdir(parents=True)
    (folder / "s1.jsonl").write_text("\n".join([
        line(type="user", isMeta=True, message={"content": "<meta>"}),
        line(type="user", message={"content": "fix the bug please"}),
    ]))
    (folder / "s2.jsonl").write_text(line(type="ai-title", aiTitle="Nice title"))
    titles = {sid: title for sid, title, _ in claude.recent_sessions(tmp_path, cwd)}
    assert titles == {"s1": "fix the bug please", "s2": "Nice title"}


def test_invite_links():
    import base64
    token = base64.b64encode(b"123456789").decode().rstrip("=") + ".x.y"
    assert app_id_from_token(token) == "123456789"
    server, user = invite_links("123")
    assert "client_id=123" in server and "scope=bot+applications.commands" in server
    assert "integration_type=1" in user
