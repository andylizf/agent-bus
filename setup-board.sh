#!/bin/bash
# Create an agent-bus v2 board, or add receipt blocks to one. Run once, as the account that will own its directories.
#   ./setup-board.sh <board-dir> <gpu-host-short-name> [--group <unix-group>] [--gpus 8] [--blocks 1000]
#   ./setup-board.sh <board-dir> --add-blocks <n>
# The board holds data only: no executable goes on it, and members install `bus` privately (bin/install.sh).
# Layout: root 0755 and shared directories 1777 (sticky). With --group the modes are 2755 and 3775.
# Setup uses directory descriptors (lib/agentbus/setup.py). A symlink or another account's directory stops it, and
# a mode or group is set only on what this run creates.
set -eu
src=$(cd "$(dirname "$0")" && pwd)
exec /usr/bin/env python3 -IB "$src/bin/bus" _setup-board --readme "$src/board-README.md" "$@"
