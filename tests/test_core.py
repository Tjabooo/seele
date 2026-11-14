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


def test_parse_marks_subagent_steps_and_skips_garbage():
    [ev] = claude.parse_line(line(type="assistant", parent_tool_use_id="t1",
                                  message={"content": [{"type": "text", "text": "inner"}]}))
    assert (ev.kind, ev.text, ev.agent) == ("text", "inner", "t1")
    assert claude.parse_line("not json") == []
    assert claude.parse_line(line(type="user", message={})) == []


def test_parse_task_events():
    [ev] = claude.parse_line(line(type="system", subtype="task_started", task_id="a1", task_type="local_agent"))
    assert ev.kind == "task_started" and ev.data["task_id"] == "a1"
    [ev] = claude.parse_line(line(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "a1"}]))
    assert ev.kind == "tasks" and ev.data["tasks"] == [{"task_id": "a1"}]
    assert claude.parse_line(line(type="system", subtype="hook_started")) == []


def test_limit_reset_from_rate_event():
    allowed = {"status": "allowed", "overageStatus": "rejected", "resetsAt": 100,
               "unifiedWindows": {"five_hour": {"status": "allowed", "resetsAt": 100}}}
    assert claude.limit_reset(allowed) is None  # overage "rejected" is not a limit
    five = {"status": "rejected", "resetsAt": 100, "unifiedWindows": {
        "five_hour": {"status": "rejected", "resetsAt": 100}, "seven_day": {"status": "allowed", "resetsAt": 900}}}
    assert claude.limit_reset(five) == 100
    both = {"status": "rejected", "unifiedWindows": {
        "five_hour": {"status": "rejected", "resetsAt": 100}, "seven_day": {"status": "rejected", "resetsAt": 900}}}
    assert claude.limit_reset(both) == 900
    assert claude.limit_reset({"status": "rejected", "resetsAt": 300}) == 300


def test_reset_from_text():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 11, 14, 15, 0, tzinfo=tz).timestamp()
    at = claude.reset_from_text("You've hit your limit · resets 7pm (Europe/Berlin)", now)
    assert datetime.fromtimestamp(at, tz) == datetime(2026, 11, 14, 19, 0, tzinfo=tz)
    at = claude.reset_from_text("5-hour limit reached ∙ resets 9:30am (Europe/Berlin)", now)
    assert datetime.fromtimestamp(at, tz) == datetime(2026, 11, 15, 9, 30, tzinfo=tz)  # tomorrow
    at = claude.reset_from_text("Weekly limit reached · resets Nov 20, 7pm (Europe/Berlin)", now)
    assert datetime.fromtimestamp(at, tz) == datetime(2026, 11, 20, 19, 0, tzinfo=tz)
    assert claude.reset_from_text("Claude AI usage limit reached|1794690600") == 1794690600
    assert claude.reset_from_text("All good") is None


def test_build_argv_live_and_user_line():
    argv = claude.build_argv("claude", live=True)
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert json.loads(claude.user_line("hi")) == {"type": "user", "message": {"role": "user", "content": "hi"}}


def test_run_closes_stdin_only_when_idle():
    import asyncio

    class Stdin:
        closed = False

        def is_closing(self):
            return self.closed

        def close(self):
            self.closed = True

        def write(self, data):
            pass

        async def drain(self):
            pass

    class Proc:
        returncode = None
        stdin = Stdin()

    def ev(kind, **data):
        return claude.Event(kind, data=data)

    async def go():
        run = claude.Run([], "p", Path("."), {}, live=True, idle_grace=0.05)
        run.proc, run.input_open = Proc(), True
        run._track(ev("init"))
        run._track(ev("tasks", tasks=[{"task_id": "a"}]))
        run._track(ev("result"))
        assert run.input_open  # an agent still runs: keep listening
        assert await run.send("more") and run.written
        run._track(ev("init"))
        run._track(ev("result"))
        assert run.input_open and not run.written
        run._track(ev("tasks", tasks=[]))  # agent done while idle: it'll usually wake Claude up...
        run._track(ev("init"))  # ...and it did
        await asyncio.sleep(0.1)
        assert run.input_open
        run._track(ev("result"))
        assert not run.input_open and Proc.stdin.closed
        assert not await run.send("too late")

        quiet = claude.Run([], "p", Path("."), {}, live=True, idle_grace=0.05)
        Proc.stdin = Stdin()
        quiet.proc, quiet.input_open = Proc(), True
        quiet._track(ev("tasks", tasks=[{"task_id": "a"}]))
        quiet._track(ev("result"))
        quiet._track(ev("tasks", tasks=[]))  # nothing wakes up: close after the grace period
        assert quiet.input_open
        await asyncio.sleep(0.1)
        assert not quiet.input_open
    asyncio.run(go())


def test_agent_card_rendering():
    from seele import agents
    card = agents.Card("t1", description="Build the reef", subagent_type="general-purpose", started=1000,
                       tools=3, tokens=12345, activity="Running tests", jump="https://x/y")
    embed = agents.card_embed(card, 1100)
    assert embed.title == "🤖 Build the reef" and embed.color.value == agents.STATUS["running"][2]
    assert "Running" in embed.description and "<t:1000:R>" in embed.description and "12.3k" in embed.description
    assert "Running tests" in embed.description and "(https://x/y)" in embed.description
    card.status, card.ended, card.runs = "done", 1130, 2
    embed = agents.card_embed(card, 2000)
    assert "Done" in embed.description and "Took 2:10" in embed.description and "run 2" in embed.description
    card.status, card.runs = "running", 3
    assert "Running again" in agents.card_embed(card, 2000).description
    assert agents.compact_line(card, 1060).startswith("🤖 Build the reef · general-purpose · ")
    assert [agents.card_status(s) for s in ("completed", "failed", "killed")] == ["done", "failed", "stopped"]
    assert agents.is_agent("local_agent") and agents.is_agent("remote_agent") and not agents.is_agent("local_bash")


def test_config_channels_and_new_fields(tmp_path):
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("[channels]\nprogress = 123\n[claude]\nlive_input = false\n")
    cfg = config.load(cfg_file)
    assert cfg.channel_id("progress") == 123 and cfg.channel_id("#progress") == 123
    assert cfg.channel_id("456") == 456 and cfg.channel_id("<#789>") == 789
    assert not cfg.live_input
    try:
        cfg.channel_id("nope")
        raise AssertionError("should fail")
    except ValueError as e:
        assert "progress" in str(e)
    st = config.State(tmp_path / "s.json")
    conv = st.get(1)
    conv.agents_channel, conv.autoresume, conv.resume_at = "55", True, 123.0
    st.agents["t1"] = {"task_id": "t1", "status": "done", "started": 1}
    st.save()
    again = config.State(tmp_path / "s.json")
    assert again.get(1).resume_at == 123.0 and again.agents["t1"]["status"] == "done"
    child = again.get(1).inherit()
    assert child.agents_channel == "55" and child.autoresume and child.resume_at == 0


def test_parse_error_result_and_rate():
    [ev] = claude.parse_line(line(type="result", subtype="error_during_execution", is_error=True,
                                  errors=["boom"]))
    assert ev.is_error and ev.text == "boom"
    [ev] = claude.parse_line(line(type="rate_limit_event", rate_limit_info={
        "unifiedWindows": {"five_hour": {"utilization": 0.42}}}))
    assert ev.kind == "rate" and ev.utilization == 0.42
    [ev] = claude.parse_line(line(type="rate_limit_event", rate_limit_info={"status": "rejected"}))
    assert ev.kind == "rate" and ev.utilization is None and ev.data["status"] == "rejected"


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
