#!/bin/bash
# Join an agent-bus board: run once, on the board's GPU host, as yourself.
# Installs your own sweep (a systemd user timer, every 30 s on that host only) that stops YOUR registered tasks when a
# GPU request to them goes unacknowledged (no watcher for 60 s, or 5 min without `bus.py ack`), replies to the
# requester, and logs GPU usage. Only your own processes can be stopped by your own sweep, which is why each member
# runs one. Then prints how to give your agent the skill.
set -eu
R=$(cd "$(dirname "$0")/.." && pwd)
. "$R/config"
[ "$(hostname -s)" = "$host" ] || { echo "run this on $host"; exit 2; }
fqdn=$(hostname)   # ConditionHost: the timer runs only here, even where home directories are shared across nodes
U=~/.config/systemd/user
mkdir -p "$U"
cat > "$U/agent-bus-sweep.service" <<EOF
[Unit]
Description=agent-bus sweep: stop my $host tasks whose GPU requests nobody acknowledges; GPU usage snapshots
ConditionHost=$fqdn

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $R/bin/bus.py sweep --reply
TimeoutStartSec=60
EOF
cat > "$U/agent-bus-sweep.timer" <<EOF
[Unit]
Description=agent-bus sweep every 30 s on $host
ConditionHost=$fqdn

[Timer]
OnBootSec=60
OnUnitActiveSec=30
AccuracySec=5

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now agent-bus-sweep.timer
echo "linger (timer keeps running when you are logged out): $(loginctl show-user "$USER" -p Linger --value)"
[ "$(loginctl show-user "$USER" -p Linger --value)" = yes ] || echo "  run: loginctl enable-linger $USER"
systemctl --user list-timers agent-bus-sweep.timer --no-pager | head -2
cat <<EOF

Give your agent the skill, on the machine where it runs:
  where the board is mounted:  mkdir -p ~/.claude/skills && ln -sfn $R/skill/agent-bus ~/.claude/skills/agent-bus
                               (Codex: mkdir -p ~/.codex/skills && ln -sfn $R/skill/agent-bus ~/.codex/skills/agent-bus)
  on your laptop:              mkdir -p ~/.claude/skills && scp -r <cluster-alias>:$R/skill/agent-bus ~/.claude/skills/
                               (Codex: the same into ~/.codex/skills/)
                               mkdir -p ~/.agent-bus && echo <cluster-alias> > ~/.agent-bus/ssh-host
                               re-copy when watch.sh reports "skill copy outdated"
Start tasks on $host with:  $R/bin/run.sh <gpus> --note "<what>" --hours <h> -- '<command>'
EOF
