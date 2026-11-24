#!/usr/bin/env bash
# Prove the container gives each user what the tool needs: transient scopes and
# services in a named slice of a delegated cgroup v2 subtree, slice kill/stop,
# linger, user timers, pidfd_open, cross-user signal isolation and /shared.
# Prints PASS/FAIL per check; exit status is the number of failures.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
AS="$HERE/as.sh"
fails=0

check() {  # check <name> <command...>: PASS if the command succeeds
    local name=$1 out; shift
    if out="$("$@" 2>&1)"; then
        echo "PASS  $name"
    else
        echo "FAIL  $name"
        printf '%s\n' "$out" | sed 's/^/      /'
        fails=$((fails + 1))
    fi
}

# Runs as <user> under bash -c; any failing line fails the check.
u() { local user=$1; shift; "$AS" "$user" bash -euo pipefail -c "$*"; }

# Shared shell helpers, prepended to each user snippet.
H='
SL=agentbus-probe.slice
CG=/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/agentbus.slice/$SL
populated() { sed -n "s/^populated //p" "$CG/cgroup.events"; }
waitfor() { for _ in $(seq 1 50); do eval "$1" && return 0; sleep 0.2; done; echo "timed out: $1"; return 1; }
cleanup() {
  systemctl --user stop $SL probe-svc.service probe-long.service probe-long2.service probe-timer.timer probe-timer.service 2>/dev/null || true
  systemctl --user reset-failed 2>/dev/null || true
}
'

for user in alice bob; do
    u "$user" "$H cleanup"

    check "$user: systemd-run --scope lands in $user's agentbus-probe.slice" u "$user" "$H
        out=\$(systemd-run --user --scope --slice=\$SL -- cat /proc/self/cgroup)
        echo \"\$out\"
        echo \"\$out\" | grep -Eq '^0::/user.slice/user-[0-9]+.slice/user@[0-9]+.service/agentbus.slice/agentbus-probe.slice/run-.*\.scope\$'"

    check "$user: transient Type=exec RemainAfterExit service, ExecMainStatus=0 Result=success" u "$user" "$H
        systemd-run --user --unit=probe-svc -p Type=exec -p RemainAfterExit=yes -- sleep 2
        waitfor '[ \"\$(systemctl --user show -p SubState --value probe-svc)\" = exited ]'
        show=\$(systemctl --user show -p ExecMainStatus,Result,ActiveState probe-svc)
        echo \"\$show\"
        echo \"\$show\" | grep -qx ExecMainStatus=0
        echo \"\$show\" | grep -qx Result=success
        echo \"\$show\" | grep -qx ActiveState=active
        systemctl --user stop probe-svc"

    check "$user: slice cgroup exists with cgroup.events populated 1 while a unit runs" u "$user" "$H
        systemd-run --user --unit=probe-long --slice=\$SL -- sleep 300
        waitfor '[ -r \$CG/cgroup.events ] && [ \"\$(populated)\" = 1 ]'
        echo \"\$CG\"; cat \$CG/cgroup.events"

    check "$user: systemctl --user kill --signal=TERM slice empties it (populated 0)" u "$user" "$H
        systemctl --user kill --signal=TERM \$SL
        waitfor '[ ! -e \$CG ] || [ \"\$(populated)\" = 0 ]'
        state=\$(systemctl --user show -p ActiveState --value probe-long)
        echo \"probe-long ActiveState=\$state\"
        [ \"\$state\" != active ]
        systemctl --user reset-failed probe-long 2>/dev/null || true"

    check "$user: systemctl --user stop slice stops its units, cgroup gone" u "$user" "$H
        systemd-run --user --unit=probe-long2 --slice=\$SL -- sleep 300
        waitfor '[ -r \$CG/cgroup.events ] && [ \"\$(populated)\" = 1 ]'
        systemctl --user stop \$SL
        waitfor '[ ! -e \$CG ]'
        [ \"\$(systemctl --user show -p ActiveState --value probe-long2)\" = inactive ]"

    check "$user: loginctl show-user -p Linger = yes" bash -c \
        "[ \"\$('$AS' $user loginctl show-user $user -p Linger)\" = Linger=yes ]"

    check "$user: per-user .timer fires" u "$user" "$H
        rm -f ~/probe-timer-fired
        systemd-run --user --unit=probe-timer --on-active=1s --timer-property=AccuracySec=100ms -- touch \$HOME/probe-timer-fired
        systemctl --user list-timers --all --no-pager | grep -q probe-timer.timer
        waitfor '[ -e ~/probe-timer-fired ]'
        rm -f ~/probe-timer-fired"

    check "$user: python3 os.pidfd_open + signal.pidfd_send_signal" "$AS" "$user" python3 -c '
import os, signal, subprocess, sys
p = subprocess.Popen(["sleep", "30"])
fd = os.pidfd_open(p.pid)
signal.pidfd_send_signal(fd, signal.SIGTERM)
rc = p.wait()
print(sys.version.split()[0], "pidfd", fd, "child rc", rc)
sys.exit(0 if rc == -signal.SIGTERM else 1)'

    u "$user" "$H cleanup"
done

check "alice cannot signal bob's processes (EPERM, bob's process survives)" bash -c "
    set -euo pipefail
    '$AS' bob systemd-run --user --unit=probe-bob-sleep -- sleep 300 >/dev/null 2>&1
    pid=\$('$AS' bob systemctl --user show -p MainPID --value probe-bob-sleep)
    echo \"bob's sleep pid \$pid\"
    [ \"\$pid\" -gt 0 ]
    '$AS' alice python3 -c '
import errno, os, signal, sys
pid = int(sys.argv[1])
for sig in (0, signal.SIGTERM, signal.SIGKILL):
    try:
        os.kill(pid, sig)
    except PermissionError:
        continue
    print(\"alice could send signal\", int(sig)); sys.exit(1)
print(\"EPERM for 0, TERM, KILL\")' \"\$pid\"
    [ \"\$('$AS' bob systemctl --user show -p ActiveState --value probe-bob-sleep)\" = active ]
    '$AS' bob systemctl --user stop probe-bob-sleep"

check "file alice creates in /shared with mode 0644 is readable by bob" bash -c "
    set -euo pipefail
    '$AS' root rm -f /shared/probe-alice.txt
    '$AS' alice bash -c 'umask 077; echo from-alice > /shared/probe-alice.txt; chmod 0644 /shared/probe-alice.txt; stat -c \"%A %U:%G %n\" /shared /shared/probe-alice.txt'
    [ \"\$('$AS' bob cat /shared/probe-alice.txt)\" = from-alice ]
    '$AS' root rm -f /shared/probe-alice.txt"

echo "failures: $fails"
exit "$fails"
