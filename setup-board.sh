#!/bin/bash
# Create (or update) an agent-bus board. Run on a node that mounts the shared directory.
#   ./setup-board.sh <board-dir> <gpu-host-short-name> [unix-group]
# Copies the tool into <board-dir>, renders the skill with the board's path and host, and makes every directory
# writable by the group so members' agents can register and leave messages. Rerun it to update a board; it never
# deletes anything there.
set -eu
[ $# -ge 2 ] || { echo "usage: $0 <board-dir> <gpu-host-short-name> [unix-group]"; exit 2; }
src=$(cd "$(dirname "$0")" && pwd)
root=$1; host=$2; group=${3:-}
umask 002
mkdir -p "$root/bin" "$root/agents" "$root/inbox/all" "$root/archive" "$root/skill/agent-bus"
cp "$src/bin/bus.py" "$src/bin/run.sh" "$src/bin/install.sh" "$root/bin/"
cp "$src/board-README.md" "$root/README.md"
sed -e "s#@ROOT@#$root#g" -e "s#@HOST@#$host#g" "$src/skill/agent-bus/SKILL.md.in" > "$root/skill/agent-bus/SKILL.md"
sed -e "s#@ROOT@#$root#g" -e "s#@HOST@#$host#g" "$src/skill/agent-bus/watch.sh" > "$root/skill/agent-bus/watch.sh"
printf 'host=%s\n' "$host" > "$root/config"
chmod 775 "$root/bin/bus.py" "$root/bin/run.sh" "$root/bin/install.sh" "$root/skill/agent-bus/watch.sh"
chmod 664 "$root/README.md" "$root/config" "$root/skill/agent-bus/SKILL.md"
for d in "$root" "$root/bin" "$root/agents" "$root/inbox" "$root/inbox/all" "$root/archive" "$root/skill" "$root/skill/agent-bus"; do
  chmod 2775 "$d" 2>/dev/null || true   # directories created by another member keep their owner's mode
done
[ -z "$group" ] || chgrp -R "$group" "$root" 2>/dev/null || echo "note: could not chgrp everything to $group (files owned by others)"
echo "board ready at $root for host $host"
echo "each member: run $root/bin/install.sh on $host once"
