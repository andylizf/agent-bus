#!/usr/bin/env bash
# Start the agentbus colima VM, build the image, (re)create the container,
# enable linger for alice and bob, and wait for both user managers.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
mkdir -p "$HERE/logs"

if ! colima status -p "$PROFILE" >/dev/null 2>&1; then
    # colima start switches the current docker context to its own; put back
    # whatever was current so other users of plain `docker` are unaffected.
    prev_ctx="$(docker context show)"
    echo "starting colima profile $PROFILE (log: $HERE/logs/colima-start.log)"
    colima start -p "$PROFILE" --cpu 4 --memory 4 --disk 20 \
        --vm-type vz --mount-type virtiofs >"$HERE/logs/colima-start.log" 2>&1
    docker context use "$prev_ctx" >/dev/null
fi

echo "building $IMAGE (log: $HERE/logs/build.log)"
"${DOCKER[@]}" build -t "$IMAGE" -f "$HERE/Containerfile" "$HERE" >"$HERE/logs/build.log" 2>&1

"${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 || true
"${DOCKER[@]}" run -d --name "$CONTAINER" --hostname "$CONTAINER" \
    --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
    --stop-signal SIGRTMIN+3 \
    -v "$REPO:/src/agent-bus:ro" \
    "$IMAGE" >/dev/null

wait_for() {  # wait_for <what> <seconds> <command...>: until the command prints running or degraded
    local what=$1 secs=$2 state=""; shift 2
    for _ in $(seq 1 "$secs"); do
        state="$("$@" 2>/dev/null || true)"
        case "$state" in running|degraded) echo "$what: $state"; return 0 ;; esac
        sleep 1
    done
    echo "$what: not up after ${secs}s (last state: ${state:-none})" >&2
    return 1
}

wait_for "system manager" 60 "${DOCKER[@]}" exec "$CONTAINER" systemctl is-system-running
"${DOCKER[@]}" exec "$CONTAINER" loginctl enable-linger alice bob
for u in alice bob; do
    wait_for "user manager $u" 60 "$HERE/as.sh" "$u" systemctl --user is-system-running
done
