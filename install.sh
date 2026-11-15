#!/usr/bin/env bash
# Install or update seele for the current user: venv, config skeleton, systemd user service.
#   ./install.sh               install and (re)start the service (this kills prompts that are running)
#   ./install.sh --no-restart  install only; `seele-restart-later` then restarts it once no prompt runs
set -euo pipefail
restart=1
for arg in "$@"; do
  case "$arg" in
    --no-restart) restart=0 ;;
    -h|--help) sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done
here="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}/seele"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/seele"
unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

python3 -m venv "$data/venv"
"$data/venv/bin/pip" install -q --upgrade "$here"
"$data/venv/bin/pip" install -q --force-reinstall --no-deps "$here"  # same version number still updates

mkdir -p "$conf" "$unit" "$HOME/.local/bin"
link="$HOME/.local/bin/seele"  # the CLI (seele post / conv / prompt / usage) on PATH
if [ ! -e "$link" ] || [ -L "$link" ]; then ln -sfn "$data/venv/bin/seele" "$link"; fi
helper="$data/seele-restart-later"  # replaced by a rename, so a copy that is waiting keeps running
install -m 755 "$here/bin/seele-restart-later" "$helper.tmp"
mv -f "$helper.tmp" "$helper"
link="$HOME/.local/bin/seele-restart-later"
if [ ! -e "$link" ] || [ -L "$link" ]; then ln -sfn "$helper" "$link"; fi
if [ ! -f "$conf/config.toml" ]; then
  install -m 600 "$here/config.example.toml" "$conf/config.toml"
  echo "Created $conf/config.toml: put your bot token in it."
fi
install -m 644 "$here/systemd/seele.service" "$unit/seele.service"
systemctl --user daemon-reload
bot_ssh="$("$data/venv/bin/python" -c 'from seele import config; print(config.load().bot_ssh)' 2>/dev/null || true)"
if [ -n "$bot_ssh" ]; then  # [bot] ssh: the bot runs on that machine; this one only has the CLI
  echo "Installed the CLI. The bot runs on $bot_ssh ([bot] ssh in $conf/config.toml), so the service stays off."
  exit 0
fi
systemctl --user enable seele.service >/dev/null
if [ "$restart" = 0 ]; then
  echo "Installed; the running service was left alone. Restart it once no prompt runs: seele-restart-later"
elif grep -qE '^token = ".+"' "$conf/config.toml" || [ -n "${DISCORD_TOKEN:-}" ]; then
  systemctl --user restart seele.service
  echo "seele (re)started: journalctl --user -u seele -f"
else
  echo "Add the token, then: systemctl --user restart seele"
fi
