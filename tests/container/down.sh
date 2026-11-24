#!/usr/bin/env bash
# Remove the container and stop the agentbus colima profile (no other profile).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
if colima status -p "$PROFILE" >/dev/null 2>&1; then
    "${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 || true
    colima stop -p "$PROFILE"
fi
