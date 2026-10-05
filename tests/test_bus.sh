#!/bin/bash
# End-to-end check of bus.py in a throwaway root: register, watch, request, unattended sweep releases the task.
# Usage: tests/test_bus.sh <scratch-dir>   (Linux: needs setsid and touch -d)
set -u
export AGENT_BUS_ROOT="$1/bus-test-$(date +%s)"   # a fresh directory per run, so nothing needs deleting
cd "$(dirname "$0")/../bin"
bus() { python3 bus.py "$@"; }

setsid sleep 300 & P=$!   # stands in for a task started by `pool.py run` (its own process group)
bus up alice-embed --gpus 1,3 --task "test task" --hours 2 --pid "$P" --on-request release --human alice
timeout 8 python3 bus.py watch alice-embed --interval 2 < /dev/null > "$1/watch.out" &
sleep 1
bus send alice-embed "need 2 gpus for 30 min to sweep" --from bob --kind request --gpus 2 --minutes 30
sleep 4
echo "--- watch printed:"; cat "$1/watch.out"
echo "--- ls:"; bus ls
sleep 5   # watch has stopped; make the heartbeat stale
touch -d '-10 minutes' "$AGENT_BUS_ROOT/agents/.heartbeat/alice-embed"
python3 -c "import json,sys;p=sys.argv[1];a=json.load(open(p));a['heartbeat']-=600;json.dump(a,open(p,'w'))" "$AGENT_BUS_ROOT/agents/alice-embed.json"
echo "--- ls (stale):"; bus ls
bus sweep --reply
sleep 1
kill -0 "$P" 2>/dev/null && echo "task pid $P still alive (FAIL)" || echo "task pid $P stopped"
echo "--- reply in bob's inbox:"; bus inbox bob --all
echo "--- log:"; cat "$AGENT_BUS_ROOT/log.jsonl"
