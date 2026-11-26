import json

from seele import hostops


def test_codex_models_and_sessions(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert hostops.run("codex_models") == []
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-a", "visibility": "list", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}]},
        {"slug": "hidden", "visibility": "hide"}]}))
    assert hostops.run("codex_models") == [["gpt-a", ["low", "ultra"], "medium"]]

    work, other = tmp_path / "work", tmp_path / "other"
    work.mkdir(), other.mkdir()
    day = home / "sessions" / "2026" / "11" / "26"
    day.mkdir(parents=True)
    for sid, cwd in (("s1", work), ("s2", other)):
        (day / f"rollout-x-{sid}.jsonl").write_text(json.dumps({"type": "session_meta",
                                                                "payload": {"id": sid, "cwd": str(cwd)}}) + "\n")
    (home / "session_index.jsonl").write_text(json.dumps({"id": "s1", "thread_name": "Fix the thing"}) + "\n")
    found = hostops.run("codex_sessions", str(work))
    assert [f[:2] for f in found] == [["s1", "Fix the thing"]]
