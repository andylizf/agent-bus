# Sourced by the scripts in this directory: one place for the names they share.
PROFILE=agentbus
CONTAINER=agentbus-rehearsal
IMAGE=agentbus-rocky9:test
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# Always this profile's socket, never the current docker context.
DOCKER=(docker --context "colima-$PROFILE")
