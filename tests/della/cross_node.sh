#!/bin/bash
# Cross-node checks of the shared filesystem from two hosts that both mount it (run from a third machine with ssh to
# both): concurrent receipt claims by link (EEXIST decides every slot once) and lockf seen across nodes, on the board's
# filesystem and on the home directory. Exits with the number of failed checks.
#   tests/della/cross_node.sh <ssh-host-a> <ssh-host-b> <test-dir-on-shared-fs> <src-dir-on-shared-fs>
set -u
A=$1 B=$2 T=$3 S=$4
fail=0
ok() { echo "PASS  $*"; }
no() { echo "FAIL  $*"; fail=$((fail + 1)); }
ssh "$A" "bash $S/setup-board.sh $T/xboard xhost --blocks 2 >/dev/null" || no "setup-board"
at=$(( $(date +%s) + 15 ))
ssh "$A" "python3 -IB $S/tests/della/xnode.py claim $T/xboard 150 a $at" > /tmp/xa.$$ &
ssh "$B" "python3 -IB $S/tests/della/xnode.py claim $T/xboard 150 b $at" > /tmp/xb.$$ &
wait
inter=$(python3 -c "import json; a=set(json.load(open('/tmp/xa.$$'))['slots']); b=set(json.load(open('/tmp/xb.$$'))['slots']); s=sorted(a|b); print(sum(1 for x,y in zip(s,s[1:]) if (x in a)!=(y in a)))")
for h in a b; do python3 -c "import json; d=json.load(open('/tmp/x$h.$$')); print('host $h', d['host'], 'ready at start+%.2f s, first claim at +%.2f s, last at +%.2f s, slots %s..%s' % (d['ready']-$at, d['first']-$at, d['last']-$at, d['slots'][:3], d['slots'][-3:]))"; done
echo "interleavings between the two hosts' slots: $inter"
v=$(ssh "$A" "python3 -IB $S/tests/della/xnode.py verify $T/xboard 300") && ok "300 concurrent claims from two nodes, every slot once: $v" || no "claims: $v"
# whether the two bursts overlap is up to the filesystem's token scheduling (GPFS often serves one node's burst
# first); the invariant checked above is that every slot was taken exactly once
echo "info  slot order switched between the hosts $inter time(s)"
for f in "$T/xlock" '$HOME/.abt-xlock'; do
  ssh "$A" "python3 -IB $S/tests/della/xnode.py hold $f 25" > /tmp/xh.$$ &
  for i in $(seq 1 30); do grep -q held /tmp/xh.$$ 2>/dev/null && break; sleep 1; done   # the hold is in place
  r=$(ssh "$B" "python3 -IB $S/tests/della/xnode.py try $f"); rc=$?
  [ $rc = 1 ] && ok "lockf on $f held on $A is seen from $B ($r)" || no "lockf on $f not seen across nodes ($r)"
  wait
  r=$(ssh "$B" "python3 -IB $S/tests/della/xnode.py try $f"); rc=$?
  [ $rc = 0 ] && ok "lockf on $f free after release ($r)" || no "lockf on $f still held ($r)"
done
ssh "$A" "rm -rf $T/xboard $T/xlock \$HOME/.abt-xlock"
rm -f /tmp/xa.$$ /tmp/xb.$$ /tmp/xh.$$
echo "failures: $fail"
exit $fail
