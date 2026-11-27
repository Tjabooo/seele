import base64

import pytest

from seele import hostops

MONITORS = [{"name": "DP-3", "activeWorkspace": {"id": 1, "name": "1"}},
            {"name": "HDMI-A-1", "activeWorkspace": {"id": 6, "name": "6"}}]
WORKSPACES = [{"id": 6, "name": "6", "lastwindowtitle": "Discord"},
              {"id": 1, "name": "1", "lastwindowtitle": "Game"},
              {"id": 3, "name": "3", "lastwindowtitle": "Terminal"},
              {"name": "special:term", "lastwindowtitle": "tjabo"}]
CLIENTS = [{"workspace": {"id": 3, "name": "3"}, "class": "Alacritty", "stableId": "1800aa", "mapped": True},
           {"workspace": {"id": 3, "name": "3"}, "class": "firefox", "stableId": "1800bb", "mapped": True},
           {"workspace": {"id": 1, "name": "1"}, "class": "game", "stableId": "1800cc", "mapped": True}]


@pytest.fixture
def hypr(monkeypatch):
    grabs = []
    monkeypatch.setattr(hostops.os, "name", "posix")
    monkeypatch.setattr(hostops.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(hostops, "_hypr_env", lambda: {})
    monkeypatch.setattr(hostops, "_hyprctl", lambda env, what: {"monitors": MONITORS, "workspaces": WORKSPACES,
                                                                 "clients": CLIENTS}[what])

    def grim(env, *args):
        grabs.append(args)
        return base64.b64encode(b"jpeg").decode()
    monkeypatch.setattr(hostops, "_grim", grim)
    return grabs


def test_workspaces_sorted_with_where_they_are(hypr):
    found = hostops.run("workspaces")
    assert [f[0] for f in found] == ["1", "3", "6", "special:term"]
    assert found[0][1] == "1 · on DP-3 · Game"
    assert found[1][1] == "3 · hidden · Terminal"


def test_every_monitor_by_default(hypr):
    shots = hostops.run("screenshot", "")
    assert [s[0] for s in shots] == ["DP-3-ws1.jpg", "HDMI-A-1-ws6.jpg"]
    assert hypr == [("-o", "DP-3"), ("-o", "HDMI-A-1")]


def test_visible_workspace_is_its_monitor(hypr):
    assert hostops.run("screenshot", "6")[0][0] == "ws6-HDMI-A-1.jpg"
    assert hypr == [("-o", "HDMI-A-1")]


def test_hidden_workspace_is_each_window(hypr):
    shots = hostops.run("screenshot", "3")
    assert [s[0] for s in shots] == ["ws3-Alacritty.jpg", "ws3-firefox.jpg"]
    assert hypr == [("-T", "1800aa"), ("-T", "1800bb")]


def test_unknown_workspace(hypr):
    with pytest.raises(RuntimeError, match="no workspace 9"):
        hostops.run("screenshot", "9")
