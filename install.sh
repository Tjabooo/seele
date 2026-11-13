#!/usr/bin/env bash
# Install or update seele for the current user: venv, config skeleton, systemd user service.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}/seele"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/seele"
unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

python3 -m venv "$data/venv"
"$data/venv/bin/pip" install -q --upgrade "$here"

mkdir -p "$conf" "$unit"
if [ ! -f "$conf/config.toml" ]; then
  install -m 600 "$here/config.example.toml" "$conf/config.toml"
  echo "Created $conf/config.toml: put your bot token in it."
fi
install -m 644 "$here/systemd/seele.service" "$unit/seele.service"
systemctl --user daemon-reload
systemctl --user enable seele.service >/dev/null
if grep -qE '^token = ".+"' "$conf/config.toml" || [ -n "${DISCORD_TOKEN:-}" ]; then
  systemctl --user restart seele.service
  echo "seele (re)started: journalctl --user -u seele -f"
else
  echo "Add the token, then: systemctl --user restart seele"
fi
