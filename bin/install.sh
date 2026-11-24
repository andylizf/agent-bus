#!/bin/bash
# Install `bus` for yourself from this checkout, pinned to a v2 board. Run as yourself; rerun to update.
#   bin/install.sh --board <v2-board-dir> [--legacy-board <old-board-dir>] [--no-timer]
# Copies this checkout's build to ~/.local/share/agent-bus/builds/<commit12>/ (chmod go-w), points ~/.local/bin/bus at
# it, records the board's path, inode and filesystem id in ~/.config/agent-bus/board.conf, and writes the adopt timer
# (bus-adopt.timer, every 30 s on the board's host only: home directories may be shared across nodes). Nothing runs
# from the board itself. Then run the setup block in README.md (enable the timer, `bus doctor`).
set -eu
src=$(cd "$(dirname "$0")/.." && pwd)
board=""; legacy=""; timer=1
while [ $# -gt 0 ]; do
  case $1 in
    --board) board=$2; shift 2 ;;
    --legacy-board) legacy=$2; shift 2 ;;
    --no-timer) timer=""; shift ;;
    *) echo "usage: $0 --board <v2-board-dir> [--legacy-board <dir>] [--no-timer]"; exit 2 ;;
  esac
done
[ -n "$board" ] || { echo "need --board <v2-board-dir>"; exit 2; }
[ -f "$board/config" ] || { echo "$board is not a v2 board (no config); setup-board.sh creates one"; exit 2; }
py=/usr/bin/python3
[ -x "$py" ] || py=$(command -v python3)
"$py" -IB "$src/bin/bus" _install --src "$src" --board "$board" ${legacy:+--legacy-board "$legacy"} >/dev/null
bin=${AGENT_BUS_BIN:-$HOME/.local/bin/bus}
host=$(sed -n 's/^host=//p' "$board/config")
here=$(hostname -s)
if [ "$here" = "$host" ]; then fqdn=$(hostname); else fqdn="$host.$(hostname -d 2>/dev/null || true)"; fqdn=${fqdn%.}; fi
echo "installed $(readlink "$bin") as $bin; board pinned in ${AGENT_BUS_CONFIG:-$HOME/.config/agent-bus}/board.conf"
if [ -n "$timer" ]; then
  name=${AGENT_BUS_TIMER:-bus-adopt}
  U=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
  mkdir -p "$U"
  envs=$(env | grep '^AGENT_BUS_' | grep -v '^AGENT_BUS_TIMER=' | sed 's/^/Environment=/' || true)
  cat > "$U/$name.service" <<UNIT
[Unit]
Description=agent-bus: supervise every run of mine on $host that has no supervisor
ConditionHost=$fqdn

[Service]
Type=oneshot
$envs
ExecStart=$py -IB $bin adopt --all
TimeoutStartSec=60
UNIT
  cat > "$U/$name.timer" <<UNIT
[Unit]
Description=agent-bus adopt every 30 s on $host
ConditionHost=$fqdn

[Timer]
OnActiveSec=10
OnUnitActiveSec=30
AccuracySec=5

[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload 2>/dev/null || echo "note: no user manager reachable now; run the setup block on $host"
  echo "adopt timer written: $U/$name.timer (ConditionHost=$fqdn); enable it: systemctl --user enable --now $name.timer"
fi
cat <<EOF2

Claude Code stop hook (so a session learns of a release of its runs), in ~/.claude/settings.json:
  "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "$bin stop-hook"}]}]}
Check the install: $bin doctor
EOF2
