#!/bin/bash
# Watch one entry's inbox on the agent-bus board: one line per new message, heartbeat while it runs, ends when the
# entry is taken down. Works on a node that mounts the board (reads it directly) or on your own machine (over ssh;
# the alias comes from $AGENT_BUS_SSH or the file ~/.agent-bus/ssh-host).
#   Claude Code: give `watch.sh <id>` to the Monitor tool; each printed line becomes a notification.
#   Codex:       `nohup watch.sh <id> --codex >> ~/.agent-bus/<id>.log 2>&1 &` queues each message into this session
#                with `codex queue --thread $CODEX_THREAD_ID`.
# Usage: watch.sh <entry-id> [--codex]
id=$1
BUS=@ROOT@/bin/bus.py
host=${AGENT_BUS_SSH:-$(cat ~/.agent-bus/ssh-host 2>/dev/null)}
if [ "$2" = --codex ]; then
  [ -n "$CODEX_THREAD_ID" ] || { echo "CODEX_THREAD_ID is not set: run this from inside the Codex session"; exit 2; }
  mkdir -p ~/.agent-bus
  exec > >(while IFS= read -r line; do
      echo "$(date '+%F %T') $line"
      case $line in
        (MSG*|"WATCH $id down"*|"WATCH $id exited"*|"WATCH $id expired"*|"WATCH $id reconnecting"*|"WATCH skill copy outdated"*)
          codex queue --thread "$CODEX_THREAD_ID" --message "agent-bus ($id): $line" 2>&1 ;;
      esac
    done) 2>&1
fi
# A laptop copy of this skill does not update itself: say so when the board's copy differs.
here=$(cd "$(dirname "$0")" && pwd)
mine=$(md5sum "$here/SKILL.md" 2>/dev/null || md5 -r "$here/SKILL.md" 2>/dev/null); mine=${mine%% *}
if [ -f @ROOT@/skill/agent-bus/SKILL.md ]; then
  theirs=$(md5sum @ROOT@/skill/agent-bus/SKILL.md); theirs=${theirs%% *}
elif [ -n "$host" ]; then
  theirs=$(ssh "$host" "md5sum @ROOT@/skill/agent-bus/SKILL.md" 2>/dev/null); theirs=${theirs%% *}
fi
[ -n "${theirs:-}" ] && [ "$mine" != "$theirs" ] && echo "WATCH skill copy outdated: copy @ROOT@/skill/agent-bus again and reread SKILL.md"
# stdin of the watch stays open while this script lives; `bus.py watch --exit-on-eof` stops when it closes, so a
# killed watcher stops the heartbeat instead of leaving the entry looking attended.
exec 3< <(exec sleep infinity); keep=$!
trap 'trap - TERM INT HUP EXIT; kill $keep $(jobs -p) 2>/dev/null; pkill -P $$ 2>/dev/null; exit 143' TERM INT HUP
trap 'kill $keep 2>/dev/null' EXIT
while true; do
  if [ -f "$BUS" ]; then
    python3 "$BUS" watch "$id" --exit-on-eof <&3 2>&1
  else
    [ -n "$host" ] || { echo "the board is not mounted here: set AGENT_BUS_SSH or write your ssh alias to ~/.agent-bus/ssh-host"; exit 2; }
    ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 "$host" "python3 $BUS watch $id --exit-on-eof" <&3 2>&1
  fi | while IFS= read -r line; do
    echo "$line"
    case $line in ("WATCH $id down"*|"no agent $id"*) exit 7 ;; esac
  done &
  wait $!
  [ $? = 7 ] && exit 0
  echo "WATCH $id reconnecting $(date +%H:%M:%S)"
  sleep 15
done
