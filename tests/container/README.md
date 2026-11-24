# Two-user Rocky Linux 9 test container

A Rocky Linux 9 container with systemd as PID 1, logind, linger and a delegated
cgroup v2 subtree per user manager, for testing code that uses `systemd --user`
transient units and slices. It runs in its own colima VM, profile `agentbus`
(4 CPUs, 4 GiB, 20 GiB disk); every script talks only to that profile's docker
context, `colima-agentbus`.

Users: `alice` (uid 2001) and `bob` (uid 2002), primary group `lab` (gid 2000).
`/shared` is `root:lab` mode 2770 (group-writable, setgid, no sticky bit).
The repository is mounted read-only at `/src/agent-bus`.

| File | What it does |
|---|---|
| `Containerfile` | The image `agentbus-rocky9:test`: systemd, dbus-broker, distro python3 3.9, the users, `/shared`, `Delegate=yes` for `user@.service`. |
| `up.sh` | Starts the colima profile if it is down, builds the image, recreates the container `agentbus-rehearsal`, enables linger for both users and waits until both user managers are `running` or `degraded`. Safe to rerun. |
| `as.sh <user> <cmd...>` | Runs a command as `alice`, `bob` or `root` in the container, from the user's home, with `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` set so `systemctl --user` works. stdin passes through; the exit status is the command's. |
| `probe.sh` | Checks scopes and services in a named slice, slice kill/stop and its `cgroup.events`, linger, a user timer, `os.pidfd_open`, cross-user signal isolation and `/shared` permissions for both users. Prints PASS/FAIL per check and exits with the failure count. |
| `down.sh` | Removes the container and stops the `agentbus` colima profile, and nothing else. |
| `lib.sh` | Names shared by the scripts (profile, container, image, docker context). |

Logs from colima start and the image build go to `logs/` (ignored by git).

The container runs with `--privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock --tmpfs /tmp --stop-signal SIGRTMIN+3`.
