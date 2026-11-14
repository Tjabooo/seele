"""The `seele` CLI: REST posting (with a fake opener) and the requests spool."""
import io
import json
import urllib.error

import pytest

from seele import __main__ as cli
from seele import config, rest


class Opener:
    def __init__(self, *errors):
        self.requests, self.errors = [], list(errors)

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.errors:
            code, body = self.errors.pop(0)
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(json.dumps(body).encode()))
        return io.BytesIO(json.dumps({"id": str(len(self.requests))}).encode())


def test_post_text_files_and_reply(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG fake")
    op = Opener()
    msgs = rest.post("TOKEN", 42, "x" * 2500, [str(shot)], reply_to=7, opener=op)
    assert [m["id"] for m in msgs] == ["1", "2"]  # split in two, files on the last one
    first, last = op.requests
    assert first.full_url.endswith("/channels/42/messages") and first.get_method() == "POST"
    assert first.headers["Authorization"] == "Bot TOKEN" and "DiscordBot" in first.headers["User-agent"]
    body = json.loads(first.data)
    assert body["message_reference"]["message_id"] == "7" and body["allowed_mentions"] == {"parse": ["users"]}
    assert last.headers["Content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="files[0]"; filename="shot.png"' in last.data and b"\x89PNG fake" in last.data
    payload = last.data.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0]
    assert json.loads(payload)["attachments"] == [{"id": 0, "filename": "shot.png"}]


def test_post_checks_files_and_explains_errors(tmp_path):
    with pytest.raises(rest.RestError, match="at most 10"):
        rest.post("T", 1, "hi", [str(tmp_path / f"{i}") for i in range(11)], opener=Opener())
    with pytest.raises(rest.RestError, match="not a file"):
        rest.post("T", 1, "hi", [str(tmp_path / "missing.png")], opener=Opener())
    big = tmp_path / "big.bin"
    big.write_bytes(b"0" * (2 * 1024 * 1024))
    with pytest.raises(rest.RestError, match="limit is 1 MB"):
        rest.post("T", 1, "", [str(big)], max_mb=1, opener=Opener())
    with pytest.raises(rest.RestError, match="nothing to post"):
        rest.post("T", 1, "  ", [], opener=Opener())
    with pytest.raises(rest.RestError, match="403: Missing Access") as e:
        rest.post("SECRET", 1, "hi", opener=Opener((403, {"message": "Missing Access", "code": 50001})))
    assert "SECRET" not in str(e.value)
    op = Opener((429, {"retry_after": 0.01}))
    assert rest.post("T", 1, "hi", opener=op)[0]["id"] == "2"  # retried after the rate limit


def test_create_thread():
    op = Opener()
    assert rest.create_thread("T", 5, "  Feature   demo ", opener=op)["id"] == "1"
    body = json.loads(op.requests[0].data)
    assert body == {"name": "Feature demo", "type": 11, "auto_archive_duration": 10080}


def test_conv_and_prompt_write_requests(tmp_path, monkeypatch, capsys):
    cfg = config.Config(channels={"lead": 77})
    monkeypatch.setattr(config, "load", lambda *a: cfg)
    monkeypatch.setattr(config, "REQUESTS_DIR", tmp_path / "req")
    assert cli.main(["conv", "--id", "lead", "--effort", "high", "--agents-channel", "88",
                     "--autoresume", "on", "--wait", "0"]) == 0
    assert cli.main(["prompt", "--id", "lead", "--new-session", "--text", "hand over", "--as", "lead",
                     "--wait", "0"]) == 0
    files = sorted((tmp_path / "req").glob("*.json"))
    conv, prompt = [json.loads(f.read_text()) for f in files]
    assert conv == {"kind": "conv", "id": 77, "set": {"effort": "high", "agents_channel": "88", "autoresume": "on"}}
    assert prompt == {"kind": "prompt", "id": 77, "text": "hand over", "new_session": True, "as": "lead"}
    assert (tmp_path / "req").stat().st_mode & 0o777 == 0o700
    assert cli.main(["conv", "--id", "nope", "--effort", "high"]) == 2
    assert cli.main(["conv", "--id", "5"]) == 2  # nothing to change
    assert "unknown channel" in capsys.readouterr().err


def test_submit_waits_for_the_bots_answer(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "REQUESTS_DIR", tmp_path)
    real = cli.write_request

    def answered(data, folder=None):
        path = real(data, folder)
        path.with_suffix(".done").write_text(json.dumps({"ok": False, "message": "unknown account 'x'"}))
        return path
    monkeypatch.setattr(cli, "write_request", answered)
    assert cli.submit({"kind": "conv"}, wait=2) == 1
    assert "unknown account" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.done"))


def test_usage_windows_from_api_and_text():
    from seele import usage
    api = {"five_hour": {"utilization": 72.0, "resets_at": "2026-11-14T19:40:00+00:00"},
           "seven_day": {"utilization": 26.0, "resets_at": "2026-11-21T17:00:00+00:00"}}
    w = usage.windows(api)
    assert w["five_hour"]["utilization"] == 0.72 and w["seven_day"]["utilization"] == 0.26
    assert w["five_hour"]["resets_epoch"] == 1794685200
    only_limits = {"limits": [{"kind": "session", "percent": 10, "resets_at": "2026-11-14T19:40:00Z"},
                              {"kind": "weekly_all", "percent": 80, "resets_at": None}]}
    w = usage.windows(only_limits)
    assert w["five_hour"]["utilization"] == 0.1 and w["seven_day"] == {
        "utilization": 0.8, "resets_at": None, "resets_epoch": None}
    text = ("You are currently using your subscription\n\n"
            "Current session: 6% used · resets Nov 14, 10:09pm (Europe/Berlin)\n"
            "Current week (all models): 2% used · resets Nov 21, 4:59pm (Europe/Berlin)\n"
            "Current week (Fable): 0% used · resets Nov 21, 5pm (Europe/Berlin)\n")
    now = 1794650000  # 2026-11-14 morning
    w = usage.windows_from_text(text, now)
    assert w["five_hour"]["utilization"] == 0.06 and w["seven_day"]["utilization"] == 0.02
    assert w["five_hour"]["resets_at"].startswith("2026-11-14T21:09")  # 10:09pm Berlin = 21:09 UTC
    assert w["seven_day"]["resets_at"].startswith("2026-11-21T15:59")


def test_usage_command(monkeypatch, capsys, tmp_path):
    from seele import usage
    cfg = config.Config()
    cfg.accounts = [config.Account("main", tmp_path / "a"), config.Account("side", tmp_path / "b")]
    monkeypatch.setattr(config, "load", lambda *a: cfg)

    async def fake(binary, config_dir, env):
        if config_dir.name == "b":
            return {"error": "not logged in"}
        return {"five_hour": {"utilization": 0.5, "resets_at": "2026-11-14T19:40:00+00:00", "resets_epoch": 1794685200},
                "seven_day": {"utilization": 0.75, "resets_at": None, "resets_epoch": None}, "source": "api"}
    monkeypatch.setattr(usage, "account_windows", fake)
    assert cli.main(["usage", "--account", "main", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["main"]["seven_day"]["utilization"] == 0.75 and "side" not in data
    assert cli.main(["usage"]) == 1  # one account failed
    out = capsys.readouterr().out
    assert "main: 5 h 50% (resets" in out and "week 75%" in out and "side: not logged in" in out
    assert cli.main(["usage", "--account", "nope"]) == 2
