#!/bin/bash
# Create an agent-bus v2 board, or add receipt blocks to one. Run once, as the account that will own its directories.
#   ./setup-board.sh <board-dir> <gpu-host-short-name> [--group <unix-group>] [--gpus 8] [--blocks 1000]
#   ./setup-board.sh <board-dir> --add-blocks <n>
# The board holds data only: no executable is copied to it, and members install `bus` privately (bin/install.sh).
# Layout (design: docs in README.md): root 2755 owned by this account; agents/ inbox/ bookings/ objections/ seq/<k>/
# archive/ log/ 3775 (setgid, sticky, group-writable), so members create their own files and only they change them.
set -eu
usage() { echo "usage: $0 <board-dir> <gpu-host> [--group G] [--gpus N] [--blocks N] | $0 <board-dir> --add-blocks N"; exit 2; }
[ $# -ge 2 ] || usage
root=$1; shift
add=""; host=""; group=""; gpus=8; blocks=1000
if [ "$1" = --add-blocks ]; then add=$2; shift 2; else host=$1; shift; fi
while [ $# -gt 0 ]; do
  case $1 in
    --group) group=$2; shift 2 ;;
    --gpus) gpus=$2; shift 2 ;;
    --blocks) blocks=$2; shift 2 ;;
    *) usage ;;
  esac
done
umask 022
mkblock() { [ -d "$root/seq/$1" ] || { mkdir "$root/seq/$1"; chmod 3775 "$root/seq/$1"; }; }
if [ -n "$add" ]; then
  [ -f "$root/config" ] && [ -d "$root/seq" ] || { echo "$root is not a v2 board"; exit 2; }
  last=$(find "$root/seq" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*' | sed 's#.*/##' | sort -n | tail -1)
  for ((k = ${last:--1} + 1; k <= ${last:--1} + add; k++)); do mkblock "$k"; done
  echo "seq blocks now 0-$((${last:--1} + add))"; exit 0
fi
[ ! -e "$root/bin/bus.py" ] || { echo "$root holds an old (v1) board; a v2 board goes in its own directory"; exit 2; }
case $host in *[!A-Za-z0-9.-]*|"") echo "bad host name"; exit 2 ;; esac
mkdir -p "$root"
chmod 2755 "$root"
for d in agents agents/.heartbeat inbox bookings objections seq archive log; do
  [ -d "$root/$d" ] || mkdir "$root/$d"
  chmod 3775 "$root/$d"
done
for ((k = 0; k < blocks; k++)); do mkblock "$k"; done
[ -f "$root/config" ] || printf 'host=%s\ngpus=%s\n' "$host" "$gpus" > "$root/config"
chmod 644 "$root/config"
src=$(cd "$(dirname "$0")" && pwd)
cp "$src/board-README.md" "$root/README.md" && chmod 644 "$root/README.md"
[ -z "$group" ] || chgrp -R "$group" "$root"
echo "v2 board ready at $root for host $host ($blocks receipt blocks)"
echo "each member: install bus privately (bin/install.sh --board $root) and run \`bus doctor\`"
