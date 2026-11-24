#!/usr/bin/env python3
"""Cross-node checks on a shared filesystem, run on two hosts at once by cross_node.sh:
  xnode.py claim <board> <n> <tag> [<start-epoch>]   claim n receipt slots (os.link EEXIST race), print them
  xnode.py verify <board> <total>      every slot 1..total present once, owner and content consistent
  xnode.py hold <file> <seconds>       hold an exclusive lockf on <file>
  xnode.py try <file>                  exit 0 if lockf on <file> is free now, 1 if another node holds it"""
import fcntl, json, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "lib"))
from agentbus import bookings, core  # noqa: E402

cmd = sys.argv[1]
if cmd == "claim":
    board, n, tag = sys.argv[2], int(sys.argv[3]), sys.argv[4]
    if len(sys.argv) > 5:                    # both hosts start at the same wall-clock time, so their claims race
        time.sleep(max(0.0, float(sys.argv[5]) - time.time()))
    ready = time.time()
    got, times = [], []
    for i in range(n):
        with core.Board(board) as b:
            got.append(bookings.claim_receipt(b, "booking", f"{core.USER}-x-{tag}-{i}", f"{i:064x}"))
        times.append(round(time.time(), 3))
    print(json.dumps(dict(host=core.HOST, slots=got, ready=round(ready, 3), first=times[0], last=times[-1])))
elif cmd == "verify":
    board, total = sys.argv[2], int(sys.argv[3])
    with core.Board(board) as b:
        recs = b.receipts()
    ids = [r["id"] for r in recs.values()]
    ok = sorted(recs) == list(range(1, total + 1)) and len(set(ids)) == total
    print(json.dumps(dict(ok=ok, slots=len(recs), unique_ids=len(set(ids)), max=max(recs, default=0))))
    sys.exit(0 if ok else 1)
elif cmd == "hold":
    f = open(sys.argv[2], "a+")
    fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print(f"held on {core.HOST}", flush=True)
    time.sleep(float(sys.argv[3]))
elif cmd == "try":
    f = open(sys.argv[2], "a+")
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(f"free on {core.HOST}")
        sys.exit(0)
    except OSError:
        print(f"held elsewhere (seen from {core.HOST})")
        sys.exit(1)
