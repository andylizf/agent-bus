#!/usr/bin/env bash
# as.sh <user> <command...>: run a command in the container as <user> (root
# included), from the user's home, with the environment the user manager needs.
# stdin is passed through; the command's exit status is this script's.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[ $# -ge 2 ] || { echo "usage: $0 <user> <command...>" >&2; exit 2; }
user=$1; shift

if [ "$user" = root ]; then
    exec "${DOCKER[@]}" exec -i -w /root "$CONTAINER" "$@"
fi
IFS=: read -r _ _ uid _ _ home _ < <("${DOCKER[@]}" exec "$CONTAINER" getent passwd "$user")
exec "${DOCKER[@]}" exec -i -u "$user" -w "$home" \
    -e HOME="$home" -e USER="$user" -e LOGNAME="$user" -e SHELL=/bin/bash \
    -e XDG_RUNTIME_DIR="/run/user/$uid" \
    -e DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    "$CONTAINER" "$@"
