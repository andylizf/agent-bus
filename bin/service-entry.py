"""Resolve the installed bus for user services, including interrupted upgrades."""
import json
import os
import sys


def main():
    installed = os.path.expanduser(os.environ.get("AGENT_BUS_BIN") or "~/.local/bin/bus")
    state = os.path.expanduser(os.environ.get("AGENT_BUS_STATE") or "~/.local/state/agent-bus")
    while True:
        bus = os.path.realpath(installed)
        try:
            with open(os.path.join(state, "auto-upgrade.json")) as stream:
                journal = json.load(stream)
                pending = journal.get("pending") if isinstance(journal, dict) else None
        except (OSError, ValueError):
            pending = None
        if bus == os.path.realpath(installed):
            break

    build = os.path.dirname(os.path.dirname(bus))
    previous = pending.get("previous") if isinstance(pending, dict) else None
    recovering = isinstance(previous, str) and (build == previous or os.path.basename(build) == pending.get("build"))
    if recovering:
        build = previous
        bus = os.path.join(build, "bin", "bus")
    action = sys.argv[1]
    if action == "_auto-upgrade-recover" and not recovering:
        return
    if action != "adopt" and not os.path.isfile(os.path.join(build, "lib", "agentbus", "auto_upgrade.py")):
        print("agent-bus: installed build predates automatic upgrades")
        return
    os.execv(sys.executable, [sys.executable, "-IB", bus, *sys.argv[1:]])


if __name__ == "__main__":
    main()
