#!/usr/bin/env bash
# Two-user rehearsal in the container (up.sh first): alice and bob each run the setup block from README.md, then
# act on one v2 board and one old (v1) board under /shared, as two members of group lab would on the GPU host.
# Checks authorship, requests across users, booking receipts, objections and conflicts, both users' backfill yielding
# to a standing booking, and the old board's send/ack/watch/exit between v1 tools and the legacy adapter.
# Exits with the number of failed checks; every check prints PASS/FAIL and the values it compared.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
A() { ./as.sh alice bash -lc "$*"; }
B() { ./as.sh bob bash -lc "$*"; }
R() { ./as.sh root bash -c "$*"; }
fail=0
pass() { echo "PASS  $*"; }
bad() { echo "FAIL  $*"; fail=$((fail + 1)); }
check() { local what=$1; shift; if "$@"; then pass "$what"; else bad "$what"; fi; }
J() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

SRC=/src/agent-bus
V2=/shared/v2
OLD=/shared/old
ENVS="export PATH=\$HOME/.local/bin:\$PATH AGENT_BUS_NVIDIA_SMI=/shared/nvidia-smi AGENT_BUS_TEST_GPUS=/shared/gpus.json"

echo "== reset"
A "systemctl --user list-units --all --plain --no-legend 'agentbus*' | awk '{print \$1}' | xargs -r -n1 systemctl --user kill --signal=KILL 2>/dev/null; systemctl --user list-units --all --plain --no-legend 'agentbus*' | awk '{print \$1}' | xargs -r -n1 systemctl --user stop 2>/dev/null; true" >/dev/null 2>&1
B "systemctl --user list-units --all --plain --no-legend 'agentbus*' | awk '{print \$1}' | xargs -r -n1 systemctl --user kill --signal=KILL 2>/dev/null; true" >/dev/null 2>&1
R "rm -rf $V2 $OLD /shared/gpus.json /shared/nvidia-smi /home/alice/.local /home/alice/.config/agent-bus /home/bob/.local /home/bob/.config/agent-bus /home/alice/.agent-bus /home/bob/.agent-bus; cp $SRC/tests/v2/fake_nvidia_smi.py /shared/nvidia-smi; chmod 755 /shared/nvidia-smi; chgrp lab /shared/nvidia-smi"
R "python3 -c 'import json; json.dump(dict(gpus=[dict(index=i,uuid=\"GPU-%d\"%i,util=50) for i in range(8)],apps=[]), open(\"/shared/gpus.json\",\"w\"))'; chmod 664 /shared/gpus.json; chgrp lab /shared/gpus.json"
HOSTS=$(A "hostname -s")

echo "== boards (alice creates both)"
A "umask 002; bash $SRC/setup-board.sh $V2 $HOSTS --group lab --blocks 3 && echo min_objection_s=60 >> $V2/config" | tail -1
A "umask 002; bash $SRC/legacy/setup-board.sh $OLD $HOSTS lab && mkdir -p $OLD/agents/.heartbeat $OLD/.sweep && chmod 2775 $OLD/agents/.heartbeat $OLD/.sweep" | tail -1

echo "== the setup block, as each member's agent runs it"
for U in A B; do
  extra=""; [ $U = A ] && extra="--legacy-board $OLD"
  $U "$ENVS; loginctl enable-linger \$USER; loginctl show-user \$USER -p Linger; $SRC/bin/install.sh --board $V2 $extra | head -2; systemctl --user enable --now bus-adopt.timer 2>&1 | tail -1; systemctl --user list-timers bus-adopt.timer --no-pager | head -2 | tail -1; bus doctor" > /tmp/setup.$U 2>&1
  u=$([ $U = A ] && echo alice || echo bob)
  sed "s/^/  [$u] /" /tmp/setup.$U
  check "$u: bus doctor passes after the setup block" grep -q "^ok   board pin" /tmp/setup.$U
  check "$u: no FAIL in bus doctor" bash -c "! grep -q '^FAIL' /tmp/setup.$U"
done

echo "== authorship (v2)"
FAKE=alice-20261124-000000-forged
B "$ENVS; cd $V2/agents && printf '%s' '{\"id\":\"$FAKE\",\"user\":\"alice\",\"host\":\"h\",\"gpus\":[0],\"class\":\"backfill\",\"booking\":null,\"run\":\"$(printf a%.0s {1..32})\",\"started\":1,\"until\":2,\"kind\":\"other\",\"exclusive\":false,\"note\":\"\",\"state\":\"running\",\"schema\":2}' > $FAKE.json && chmod 644 $FAKE.json"
n=$(A "$ENVS; bus ls --json" | J "len([e for e in d['entries'] if e['id']=='$FAKE'])")
check "a record bob wrote claiming user alice is not read (seen: $n)" test "$n" = 0
B "rm -f $V2/agents/$FAKE.json"

A "$ENVS; bus run --gpus 0 --hours 2 --note 'alice backfill' --detach --info-file /tmp/a1.json -- 'sleep 600' >/dev/null"
AID=$(A "cat /tmp/a1.json" | J "d['id']"); ARUN=$(A "cat /tmp/a1.json" | J "d['run']")
sleep 2
own=$(A "stat -c %U:%a $V2/agents/$AID.json")
check "alice's entry is alice's, 0644 ($own)" test "$own" = "alice:644"
B "rm -f $V2/agents/$AID.json 2>/dev/null; mv $V2/agents/$AID.json /tmp/x 2>/dev/null; echo x > $V2/agents/$AID.json 2>/dev/null"; still=$(A "stat -c %U $V2/agents/$AID.json")
check "bob cannot remove, move or overwrite alice's entry (owner now: $still)" test "$still" = alice

echo "== requests across users"
B "$ENVS; bus send $AID --kind request --gpus 1 --minutes 30 --note 'need gpu0 for eval' --json" > /tmp/req.json
REQ=$(J "d['request_id']" < /tmp/req.json); BOX=$(J "d['reply_to']" < /tmp/req.json)
st=""; for i in $(seq 1 40); do st=$(A "$ENVS; bus state $AID" | J "d['state']"); [ "$st" = ended ] && break; sleep 1; done
check "bob's request stops alice's backfill (state $st after ${i}s)" test "$st" = ended
why=$(A "$ENVS; bus state $AID" | J "d['end_reason']")
echo "  end_reason: $why"
rep=$(B "$ENVS; cat $V2/inbox/$BOX/*.json" | python3 -c "import json,sys; t=sys.stdin.read().replace('}\n{','}\x00{').split('\x00'); print([json.loads(x)['note'] for x in t if '$REQ' in x])")
check "bob's mailbox has alice's reply: $rep" grep -q "released: GPUs 0 free of this job" <<<"$rep"
rown=$(B "stat -c %U $V2/inbox/$BOX/* | sort -u")
check "the reply file is alice's ($rown)" test "$rown" = alice

A "$ENVS; bus run --gpus 1 --hours 2 --note 'alice second' --detach --info-file /tmp/a2.json -- 'sleep 600' >/dev/null"
AID2=$(A "cat /tmp/a2.json" | J "d['id']"); sleep 2
B "$ENVS; bus send $AID2 --kind request --gpus 1 --minutes 5 --json > /tmp/r2.json; f=\$(ls $V2/inbox/$AID2/*.json); chmod 664 \$f"
sleep 3
st=$(A "$ENVS; bus state $AID2" | J "d['state']"); adv=$(A "$ENVS; bus state $AID2" | J "list(d['requests'].values())")
check "a group-writable request from bob is advisory (state $st; $adv)" test "$st" = running

echo "== bookings: receipts, objections, conflicts"
T=$(date +%s)
A "$ENVS; bus book --gpus 2 --from $((T + 3600)) --to $((T + 7200)) --objection 10m --note 'alice kernel timing' --json" > /tmp/b1.json
B "$ENVS; bus book --gpus 2,3 --from $((T + 5400)) --to $((T + 9000)) --objection 10m --note 'bob eval' --json" > /tmp/b2.json
B1=$(J "d['id']" < /tmp/b1.json); S1=$(J "d['slot']" < /tmp/b1.json); B2=$(J "d['id']" < /tmp/b2.json); S2=$(J "d['slot']" < /tmp/b2.json)
check "receipts: distinct slots in publication order (alice $S1, bob $S2)" test "$S1" -lt "$S2"
ro=$(A "stat -c '%n %U %a' $V2/seq/0/$S1 $V2/seq/0/$S2" | tr '\n' ' ')
check "each receipt is its author's, 0644 ($ro)" bash -c "grep -q '/$S1 alice 644' <<<'$ro' && grep -q '/$S2 bob 644' <<<'$ro'"
s2=$(A "$ENVS; bus cal --json" | J "[b['state'] for b in d['bookings'] if b['id']=='$B2'][0]")
s2b=$(B "$ENVS; bus cal --json" | J "[b['state'] for b in d['bookings'] if b['id']=='$B2'][0]")
check "bob's later overlapping booking is conflicting for both readers ($s2 / $s2b)" bash -c "[ $s2 = conflicting ] && [ $s2b = conflicting ]"
r=$(B "$ENVS; bus object $B1 2>&1"); check "an objection without a reason is rejected ($r)" grep -q "reason" <<<"$r"
B "$ENVS; bus object $B1 --reason 'deadline run on gpu2 tonight' --json" > /tmp/o1.json
s1=$(A "$ENVS; bus cal --json" | J "[(b['state'], b['objections'][0]['reason']) for b in d['bookings'] if b['id']=='$B1'][0]")
check "bob's reasoned objection makes alice's booking objected: $s1" grep -q objected <<<"$s1"
s2=$(A "$ENVS; bus cal --json" | J "[b['state'] for b in d['bookings'] if b['id']=='$B2'][0]")
check "with alice's booking objected, bob's is no longer conflicting ($s2)" test "$s2" = pending
B "chmod 600 $V2/bookings/$B2.json; touch -d '-1 day' $V2/bookings/$B2.json; chmod 644 $V2/bookings/$B2.json"
s2c=$(A "$ENVS; bus cal --json" | J "[(b['state'],b['slot']) for b in d['bookings'] if b['id']=='$B2'][0]")
check "chmod/touch on bob's booking changes nothing for alice's reader ($s2c)" grep -q "'pending', $S2" <<<"$s2c"
r=$(B "rm -f $V2/bookings/$B1.json 2>&1; ls $V2/bookings/$B1.json")
check "bob cannot remove alice's booking" grep -q "$B1.json" <<<"$r"

echo "== a standing booking's start: both users' backfill yield"
A "$ENVS; bus run --gpus 5 --hours 30 --note 'alice long backfill' --detach --info-file /tmp/a5.json -- 'sleep 600' >/dev/null"
B "$ENVS; bus run --gpus 5,6 --hours 30 --note 'bob long backfill' --detach --info-file /tmp/b5.json -- 'sleep 600' >/dev/null"
A5=$(A "cat /tmp/a5.json" | J "d['id']"); B5=$(B "cat /tmp/b5.json" | J "d['id']")
sleep 3
T=$(date +%s)
# this board's minimum objection window is 1 min (config min_objection_s=60); alice publishes with it
A "$ENVS; bus book --gpus 5 --from $T --to $((T + 3600)) --objection 1m --note 'alice exclusive timing' --json" > /tmp/b5b.json
BK=$(J "d['id']" < /tmp/b5b.json)
r=$(B "$ENVS; bus run --gpus 5 --detach -- 'sleep 5' 2>&1"); echo "  bob's new backfill while pending: $(head -c 80 <<<"$r")"
if grep -q "inside" <<<"$r"; then bad "a pending booking blocked bob's new backfill: $r"; else pass "a pending booking does not block bob's new backfill"; fi
echo "  waiting for the 1 min objection window of $BK"
for i in $(seq 1 40); do
  sa=$(A "$ENVS; bus state $A5" | J "d['state']"); sb=$(B "$ENVS; bus state $B5" | J "d['state']")
  [ "$sa" = ended ] && [ "$sb" = ended ] && break; sleep 5
done
ra=$(A "$ENVS; bus state $A5" | J "d['end_reason']"); rb=$(B "$ENVS; bus state $B5" | J "d['end_reason']")
check "alice's backfill yielded: $ra" test "$ra" = "yielded to booking $BK"
check "bob's backfill yielded: $rb" test "$rb" = "yielded to booking $BK"
r=$(B "$ENVS; bus run --gpus 5 --detach -- 'sleep 5' 2>&1")
check "bob's new backfill inside alice's standing booking is refused: $(head -c 90 <<<"$r")" grep -q "standing booking $BK" <<<"$r"

echo "== wrong-owner heartbeat"
HID=alice-20261124-000001-hb
A "cd $V2/agents && printf '%s' '{\"id\":\"$HID\",\"user\":\"alice\",\"host\":\"h\",\"gpus\":[7],\"class\":\"backfill\",\"booking\":null,\"run\":\"$(printf b%.0s {1..32})\",\"started\":1,\"until\":2,\"kind\":\"other\",\"exclusive\":false,\"note\":\"\",\"state\":\"running\",\"schema\":2}' > $HID.json && chmod 644 $HID.json"
B "touch $V2/agents/.heartbeat/$HID"
occ=$(A "$ENVS; bus ls --json" | J "[e['occupancy'] for e in d['entries'] if e['id']=='$HID'][0]")
check "a heartbeat bob made for alice's entry does not count (occupancy $occ)" test "$occ" = unknown
A "rm -f $V2/agents/$HID.json"

echo "== old board: v1 tools and the adapter across users"
B "cd /tmp && $OLD/bin/run.sh 3 --note 'bob v1 job' --hours 1 -- 'sleep 20'" > /tmp/bobrun.txt
BID=$(B "ls $OLD/agents | grep '^bob-' | head -1 | sed 's/.json\$//'")
echo "  bob's v1 entry: $BID"
seen=$(A "$ENVS; bus legacy ls --json" | J "[(e['id'], e['file_owner'], e['state']) for e in d]")
check "alice's adapter lists bob's v1 entry: $seen" grep -q "'$BID', 'bob'" <<<"$seen"
A "$ENVS; bus legacy send $BID 'may I take gpu 3 at 18:00' --from alice --kind request --gpus 1 --minutes 60 >/dev/null"
w=$(B "timeout 20 python3 $OLD/bin/bus.py watch $BID --interval 1 < /dev/null")
check "bob's v1 watch prints alice's adapter message and exits" grep -q "may I take gpu 3" <<<"$w"
MID=$(grep -o 'MSG [^ ]*' <<<"$w" | head -1 | cut -d' ' -f2)
B "python3 $OLD/bin/bus.py ack $BID $MID && python3 $OLD/bin/bus.py send alice 'yes, after 18:00' --from $BID --kind reply --reply-to $MID >/dev/null"
inb=$(A "$ENVS; bus legacy inbox alice --all")
check "alice's adapter reads bob's v1 reply: $(head -c 100 <<<"$inb")" grep -q "yes, after 18:00" <<<"$inb"
un=$(A "$ENVS; bus legacy inbox $BID --json" | J "len(d)")
check "bob's ack is honored by the adapter's unread view ($un unread)" test "$un" = 0
A "$ENVS; bus legacy sweep-own --reply --json" > /tmp/sw.json
if grep -q bob /tmp/sw.json; then bad "alice's sweep-own touched bob's entry: $(cat /tmp/sw.json)"; else pass "alice's sweep-own leaves bob's entry alone ($(J "d['archived']" < /tmp/sw.json))"; fi
for i in $(seq 1 30); do B "test -f $OLD/agents/$BID.json" || break; sleep 1; done
check "bob's v1 job exited and run.sh took its entry down (${i}s)" bash -c "! ./as.sh bob test -f $OLD/agents/$BID.json"
arch=$(A "ls $OLD/archive | grep -c '^$BID'")
check "bob's entry is in the archive ($arch)" test "$arch" = 1

A "nohup sleep 300 >/dev/null 2>&1 & echo \$! > /tmp/apid"
APID=$(A "cat /tmp/apid")
LID=alice-20261124-000002-legacyjob
A "$ENVS; bus legacy up $LID --gpus 4 --task 'alice legacy-adapter job' --hours 1 --pid $APID >/dev/null"
mode=$(A "stat -c %a $OLD/agents/$LID.json")
v1=$(B "python3 $OLD/bin/bus.py ls --json" | J "[(e['id'], e['user'], e['gpus']) for e in d if e['id']=='$LID']")
check "bob's v1 ls reads alice's adapter entry ($v1, mode $mode)" grep -q "'$LID', 'alice', '4'" <<<"$v1"
B "python3 $OLD/bin/bus.py send $LID 'please free gpu4' --from bob-asker --kind request --gpus 1 --minutes 10 >/dev/null"
A "python3 - <<'PY'
import json, os, time
p='$OLD/agents/$LID.json'; a=json.load(open(p)); a['heartbeat']=int(time.time())-600
os.unlink(p); open(p,'w').write(json.dumps(a)); os.chmod(p, 0o644)
os.utime('$OLD/agents/.heartbeat/$LID', (time.time()-90, time.time()-90))
PY"
A "$ENVS; bus legacy sweep-own --reply --json" > /tmp/sw2.json
check "sweep-own stops alice's unwatched legacy job through its identity record ($(J "d['stopped']" < /tmp/sw2.json))" grep -q "$LID" /tmp/sw2.json
gone=$(A "kill -0 $APID 2>/dev/null && echo alive || echo gone")
check "the recorded process got the TERM ($gone)" test "$gone" = gone
rb=$(B "python3 $OLD/bin/bus.py inbox bob-asker --all")
check "bob's v1 inbox has the stop reply: $(head -c 110 <<<"$rb")" grep -q "was stopped" <<<"$rb"

echo "== cleanup"
for rid in $AID2; do A "$ENVS; bus release $rid --wait 40 --json >/dev/null"; done
A "$ENVS; bus runs --json" | J "[(r['id'][-24:], r['state']) for r in d]"
B "$ENVS; bus runs --json" | J "[(r['id'][-24:], r['state']) for r in d]"
echo "failures: $fail"
exit $fail
