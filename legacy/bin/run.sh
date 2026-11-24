#!/bin/bash
# Start a GPU task on the board's GPU host and register it on the agent-bus board, in one step.
#
#   run.sh <gpu-indices> --note "<what this is>" --hours <expected runtime> [--on-request agent|release|"keep: <reason>"]
#          [--exclusive] -- '<command>'
#
# Run it on the GPU host (named in the board's `config`). The command runs detached in its own process group with CUDA_VISIBLE_DEVICES set; its output
# goes to ~/.agent-bus/runs/<id>.log. A SIGTERM to the printed pid (what the board's sweep sends) stops the command
# and takes the entry down; so does the command exiting. Prints the entry id: watch its inbox with
# `bus.py watch <id>` (see the agent-bus skill).
set -eu
R=$(cd "$(dirname "$0")/.." && pwd)
BUS=$R/bin/bus.py
. "$R/config"
[ "$(hostname -s)" = "$host" ] || { echo "run.sh starts work on $host; run it there"; exit 2; }
gpus=$1; shift
note=""; hours=""; policy=agent; excl=""
while [ $# -gt 0 ]; do
  case $1 in
    --note) note=$2; shift 2 ;;
    --hours) hours=$2; shift 2 ;;
    --on-request) policy=$2; shift 2 ;;
    --exclusive) excl=--exclusive; shift ;;
    --) shift; break ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[ -n "$note" ] && [ -n "$hours" ] && [ $# -eq 1 ] || { echo "need --note, --hours and one quoted command after --"; exit 2; }
cmd=$1
slug=$(printf '%s' "$note" | tr 'A-Z' 'a-z' | tr -cs 'a-z0-9' '-' | cut -c1-30 | sed 's/-*$//')
id="$USER-$(date +%Y%m%d-%H%M%S)-$slug"
mkdir -p ~/.agent-bus/runs
printf '%s\n' "$cmd" > ~/.agent-bus/runs/"$id".cmd
# The wrapper forwards a TERM to the command's own process group, waits for it, then takes the entry down with a
# reason saying whether it was stopped or exited on its own.
inner="export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=$gpus AGENT_BUS_ID=$id
trap 'why=\"stopped by TERM, \"; kill -TERM -\$c 2>/dev/null' TERM INT
setsid bash ~/.agent-bus/runs/$id.cmd & c=\$!
wait \$c; rc=\$?
kill -0 \$c 2>/dev/null && { wait \$c; rc=\$?; }
python3 $BUS down $id --reason \"\${why:-run exited }rc=\$rc\" >/dev/null 2>&1
echo \"[agent-bus] exit=\$rc at \$(date -Is)\""
setsid nohup bash -c "$inner" > ~/.agent-bus/runs/"$id".log 2>&1 < /dev/null &
pid=$!
python3 $BUS up "$id" --gpus "$gpus" --task "$note" --hours "$hours" --pid "$pid" --on-request "$policy" $excl | head -1
echo "pid $pid  log ~/.agent-bus/runs/$id.log"
