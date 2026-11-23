#!/usr/bin/env python3
"""agent-bus: a shared directory where agents that started work on a shared GPU box say what they hold and take messages.

No server. Each running task registers itself as an agent (agents/<id>.json: who, which GPUs, until when, what
happens on a request for its GPUs); anyone, person or agent, drops a message file into inbox/<id>/; the agent
that owns the task runs `bus.py watch <id>`, which refreshes its heartbeat while it waits, prints the unacknowledged
messages as one line each once there are any, and exits, so the agent wakes up, answers with `bus.py send`, acks,
and starts the watch again (`--follow` keeps printing instead of exiting). One watch runs per entry. The watch polls
(every 5 s): GPFS does not deliver inotify events for files written from another node. `bus.py stop-hook` is a Claude
Code Stop hook that keeps a session from ending its turn while an entry it registered has no watch. Every write is a
new file or an atomic rename, so concurrent writers never corrupt each other. Python 3.9, standard library only.
"""
import argparse, fcntl, getpass, json, math, os, re, secrets, select, signal, socket, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("AGENT_BUS_ROOT") or Path(__file__).resolve().parent.parent)  # the board is the directory above bin/
STALE = 60           # seconds without a heartbeat before an agent counts as unattended (watch beats every 5 s)
RESPOND = 300        # seconds a watched agent has to act on a request before it counts as unresponsive
SNAPSHOT_EVERY = 120 # seconds between GPU usage snapshots written by the sweep
IDLE_UTIL = 5        # percent: an entry whose GPUs are all below this is idle
IDLE_WARN = int(os.environ.get("AGENT_BUS_IDLE_WARN", 1800))  # seconds idle before the owner's agent is warned, and between warnings
TZ = ZoneInfo(os.environ.get("AGENT_BUS_TZ", "America/New_York"))  # booking times are read and shown in this zone
NGPUS = int(os.environ.get("AGENT_BUS_GPUS", 8))  # GPUs on the host, the rows of `cal`
STOP_BLOCKS = 3      # stop-hook: blocks in a row within one turn before it lets the turn end anyway
DIR_MODE = 0o2775    # the board's group may create files in every directory
FILE_MODE = 0o664


def now():
    return int(time.time())


def stamp(t=None):
    return time.strftime("%m-%d %H:%M", time.localtime(t or now()))


def mkdir(p):
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, DIR_MODE)
    except PermissionError:  # someone else's directory, already set up by them
        pass


def write_json(path, obj):
    mkdir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    os.chmod(tmp, FILE_MODE)
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def event(action, **kw):
    """One line per action in log.jsonl, the bus's own audit trail."""
    mkdir(ROOT)
    line = json.dumps(dict(ts=now(), user=getpass.getuser(), action=action, **kw), ensure_ascii=False)
    fd = os.open(ROOT / "log.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def agent_path(aid):
    return ROOT / "agents" / f"{aid}.json"


def state(a):
    if a.get("pid") and a.get("host") == socket.gethostname().split(".")[0]:
        try:
            os.kill(int(a["pid"]), 0)
        except ProcessLookupError:
            return "exited"
        except PermissionError:
            pass
    if not attended(a):
        return "unattended"
    return "expired" if a.get("until") and a["until"] < now() else "live"


def hb_path(aid):
    return ROOT / "agents" / ".heartbeat" / aid


def beat(aid):
    p = hb_path(aid)
    mkdir(p.parent)
    p.touch()
    try:
        os.chmod(p, FILE_MODE)
    except PermissionError:
        pass


def attended(a):
    try:
        last = hb_path(a["id"]).stat().st_mtime
    except OSError:
        last = 0
    return now() - max(last, a.get("heartbeat", 0)) <= STALE  # the JSON field is what watchers before .heartbeat/ wrote


def up(args):
    aid = args.id
    old = read_json(agent_path(aid)) or {}
    a = dict(old, id=aid, user=getpass.getuser(), human=args.human or old.get("human") or getpass.getuser(),
             host=args.host or old.get("host") or socket.gethostname().split(".")[0],
             gpus=args.gpus if args.gpus is not None else old.get("gpus", ""),
             task=args.task or old.get("task", ""), pid=args.pid or old.get("pid"),
             until=now() + int(args.hours * 3600) if args.hours else old.get("until"),
             on_request=args.on_request or old.get("on_request", "agent"),
             exclusive=args.exclusive if args.exclusive is not None else old.get("exclusive", False),  # --shared sets False
             started=old.get("started", now()), heartbeat=now(), seen=old.get("seen", []),
             session=args.session or os.environ.get("CLAUDE_CODE_SESSION_ID") or old.get("session"))  # for stop-hook
    write_json(agent_path(aid), a)
    beat(aid)
    mkdir(ROOT / "inbox" / aid)
    event("up", agent=aid, gpus=a["gpus"], host=a["host"], task=a["task"], until=a["until"], on_request=a["on_request"],
          exclusive=a["exclusive"], pid=a["pid"])
    print(f"agent {aid} up: {a['host']} gpus={a['gpus']} until={stamp(a['until']) if a['until'] else '-'} "
          f"on_request={a['on_request']} {'exclusive' if a['exclusive'] else 'shared'}\nwatch: python3 {ROOT}/bin/bus.py watch {aid}")


def archive(p, a, reason):
    """Move an entry to archive/ with when and why it went down. False when another process moved it first."""
    mkdir(ROOT / "archive")
    dest = ROOT / "archive" / f"{a['id']}-{now()}.json"
    try:
        os.replace(p, dest)  # the rename is the claim: of two sweeps taking the same entry down, one wins
    except FileNotFoundError:
        return False
    write_json(dest, dict(a, down_at=now(), down_reason=reason))
    hb_path(a["id"]).unlink(missing_ok=True)
    return True


def down(args):
    p = agent_path(args.id)
    a = read_json(p)
    if not a:
        sys.exit(f"no agent {args.id}")
    if a["user"] != getpass.getuser():
        sys.exit(f"{args.id} belongs to {a['user']}; only its owner takes it down")
    archive(p, a, args.reason)
    event("down", agent=args.id, reason=args.reason)
    print(f"agent {args.id} down ({args.reason})")


def ls(args):
    rows = []
    for p in sorted((ROOT / "agents").glob("*.json")):
        a = read_json(p)
        if a:
            rows.append(a)
    if args.json:
        print(json.dumps([dict(a, state=state(a)) for a in rows], indent=1, ensure_ascii=False))
        return
    if not rows:
        print("no agents registered")
    for a in rows:
        unread = len(unread_msgs(a))
        print(f"{a['id']:<34} {a['user']:<7} {a['host']}:{a.get('gpus') or '-':<8} {state(a):<10} "
              f"until {stamp(a['until']) if a.get('until') else '-':<11} on_request={a.get('on_request')} "
              f"{'exclusive' if a.get('exclusive') else 'shared'} pid={a.get('pid') or '-'}  "
              f"unread={unread}  {a.get('task', '')[:70]}")
    soon = [b for b in bookings() if b["end"] > now() and b["start"] < now() + 86400]
    if soon:
        print("bookings, next 24 h (`bus.py cal` for the timeline):")
    for b in soon:
        print(f"  gpus {gpu_text(b['gpus']):<7} {when(b['start'])} - {when(b['end'])}  {b['name']} ({b['user']})"
              f"  {b.get('note', '')[:70]}  [{b['id']}]")


def msgs(aid):
    out = []
    for box in (ROOT / "inbox" / aid, ROOT / "inbox" / "all"):
        for p in sorted(box.glob("*.json")) if box.is_dir() else []:
            m = read_json(p)
            if m and m.get("from") != aid:
                out.append(m)
    return sorted(out, key=lambda m: m["ts"])


def unread_msgs(a):
    seen = set(a.get("seen", []))
    return [m for m in msgs(a["id"]) if m["id"] not in seen and m["ts"] >= a.get("started", 0)]


def send(args):
    if args.kind != "reply" and args.to != "all" and not agent_path(args.to).exists() and not (ROOT / "inbox" / args.to).is_dir():
        sys.exit(f"no agent or inbox {args.to}; `bus.py ls` lists them")
    if args.text == "-":
        args.text = sys.stdin.read().strip()
    mid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    sender = args.sender or getpass.getuser()
    m = dict(id=mid, ts=now(), from_user=getpass.getuser(), to=args.to, kind=args.kind, text=args.text,
             reply_to=args.reply_to, gpus=args.gpus, minutes=args.minutes)
    m["from"] = sender
    write_json(ROOT / "inbox" / args.to / f"{mid}.json", m)
    event("send", msg=mid, frm=sender, to=args.to, kind=args.kind, text=args.text[:200])
    print(f"sent {mid} to {args.to}")


def fmt(m):
    extra = "".join(f" {k}={m[k]}" for k in ("gpus", "minutes", "reply_to") if m.get(k))
    text = str(m["text"]).replace("\r", " ").replace("\n", "\\n")  # one line per message: a newline in the text can never pose as a MSG or WATCH line
    return f"MSG {m['id']} {stamp(m['ts'])} from={m['from']} ({m['from_user']}) to={m.get('to')} kind={m['kind']}{extra}: {text}"


def inbox(args):
    a = read_json(agent_path(args.id)) or {"id": args.id, "seen": [], "started": 0}
    ms = msgs(args.id) if args.all else unread_msgs(a)
    for m in ms:
        print(fmt(m))
    if not ms:
        print("no messages" if args.all else "no unread messages")


def lock_path(aid):
    return ROOT / "agents" / ".watch" / f"{aid}.lock"


def take_watch_lock(aid):
    """The open lock file once this process holds the entry's watch lock, or None when another watch holds it.
    A POSIX lock (lockf), not flock: on GPFS flock is local to one node, and a watch on another node would not see it.
    The file must stay open, and must never be opened and closed again in this process, or the lock goes with it."""
    p = lock_path(aid)
    mkdir(p.parent)
    f = open(p, "a+")
    try:
        os.chmod(p, FILE_MODE)
    except PermissionError:
        pass
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.truncate(0)
    f.write(f"{os.getpid()} {socket.gethostname().split('.')[0]}\n")
    f.flush()
    return f


def watcher(aid):
    """'<pid> <host>' of the watch holding the entry's lock, or None when no watch runs."""
    p = lock_path(aid)
    try:
        f = open(p, "a+")
    except OSError:
        return None
    with f:
        try:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            return f.read().strip() or "?"
        fcntl.lockf(f, fcntl.LOCK_UN)
        return None


def watch(args):
    """Wait for messages to the entry, keeping its heartbeat; print every unacknowledged one and exit 0. An unacked
    message is printed again by the next watch, so ack each once handled. With --follow (or --exit-on-eof), keep
    printing each new message instead, until the entry is taken down. One watch per entry: a second exits 3."""
    follow = args.follow or args.exit_on_eof
    p = agent_path(args.id)
    a = read_json(p)
    if not a:
        sys.exit(f"no agent {args.id}; `bus.py up` first")
    if a["user"] != getpass.getuser():
        sys.exit(f"{args.id} belongs to {a['user']}")
    lock = take_watch_lock(args.id)
    if not lock:
        print(f"WATCH {args.id} already watched by {watcher(args.id) or 'another watch'} (pid host); this one exits", flush=True)
        sys.exit(3)
    said_path = lock_path(args.id).with_suffix(".said")  # the expired/exited notice is given once per entry, not per watch
    try:
        said = said_path.read_text().strip()
    except OSError:
        said = None
    if follow:
        print(f"WATCH {args.id} up; {len(unread_msgs(a))} unread", flush=True)
    printed = set()
    while True:
        a = read_json(p)
        if not a:
            print(f"WATCH {args.id} down: agent file removed", flush=True)
            return
        beat(args.id)
        new = [m for m in unread_msgs(a) if m["id"] not in printed]
        for m in new:
            print(fmt(m), flush=True)
            printed.add(m["id"])
        st = state(a)
        if st not in ("exited", "expired") and said:  # extended (`up --hours`): a later expiry is announced again
            said = None
            said_path.unlink(missing_ok=True)
        if st in ("exited", "expired") and st != said:
            said = st
            said_path.write_text(st)
            print(f"WATCH {args.id} {st}: take it down with `bus.py down {args.id}` or extend with `bus.py up {args.id} --hours N`", flush=True)
            if not follow:
                return
        if new and not follow:
            print(f"WATCH {args.id}: ack each message once handled (`bus.py ack {args.id} <msg-id>...`; an unacked one "
                  f"is printed again), then start `bus.py watch {args.id}` again", flush=True)
            return
        if args.exit_on_eof:
            r, _, _ = select.select([sys.stdin], [], [], args.interval)
            if r and not sys.stdin.read(1):
                return
        else:
            time.sleep(args.interval)


def stop_hook(args):
    """Claude Code Stop hook: block the turn from ending while an entry this session registered (`up` records
    $CLAUDE_CODE_SESSION_ID) is on the board and no watch holds its lock, with the command that starts one. At most
    STOP_BLOCKS blocks in a row within one turn; every other session, and one whose entries are all watched, stops."""
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return
    sid = hook.get("session_id")
    if not sid:
        return
    me = getpass.getuser()
    need = []
    for p in sorted((ROOT / "agents").glob("*.json")):
        a = read_json(p)
        if a and a.get("user") == me and a.get("session") == sid and state(a) != "exited" and not watcher(a["id"]):
            need.append(a["id"])
    counter = Path.home() / ".agent-bus" / "stop-hook" / f"{sid}.json"
    # stop_hook_active: this stop follows a block of ours in the same turn; a new turn starts the count again
    blocks = (read_json(counter) or {}).get("blocks", 0) if hook.get("stop_hook_active") else 0
    if not need:
        counter.unlink(missing_ok=True)
        return
    if blocks >= STOP_BLOCKS:  # the session did not start a watch after STOP_BLOCKS reminders: let the turn end
        return
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text(json.dumps({"blocks": blocks + 1, "entries": need}))
    cmds = "; ".join(f"`python3 {ROOT}/bin/bus.py watch {i}`" for i in need)
    print(json.dumps({"decision": "block", "reason":
        f"agent-bus: {', '.join(need)} {'is' if len(need) == 1 else 'are'} on the board with no watch running, so a GPU "
        f"request to {'it' if len(need) == 1 else 'them'} would go unanswered. Start the watch with the Bash tool, "
        f"run_in_background true: {cmds}. It exits when a message arrives: handle it, ack it (`bus.py ack <id> "
        f"<msg-id>`), and start the watch again. Then end the turn. If the task has ended, `bus.py down <id>` instead."}))


def ack(args):
    """Mark messages read once the agent has answered or acted on them."""
    p = agent_path(args.id)
    a = read_json(p)
    if not a:
        sys.exit(f"no agent {args.id}")
    a["seen"] = sorted(set(a.get("seen", [])) | set(args.msg))
    write_json(p, a)
    event("ack", agent=args.id, msgs=args.msg)


def snapshot(host):
    """One line in usage.jsonl: every GPU on this host, its use, the processes on it and whose entry claims it."""
    last = ROOT / ".last-snapshot"
    try:
        if now() - last.stat().st_mtime < SNAPSHOT_EVERY:
            return
    except OSError:
        pass
    q = lambda *a: subprocess.run(["nvidia-smi", *a, "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    gpus = {}
    for line in q("--query-gpu=index,uuid,utilization.gpu,memory.used,memory.total").splitlines():
        i, uuid, util, used, total = [x.strip() for x in line.split(",")]
        gpus[uuid] = dict(gpu=int(i), util=int(util), mem_mb=int(used), total_mb=int(total), procs=[], claimed_by=[])
    for line in q("--query-compute-apps=gpu_uuid,pid,used_memory").splitlines():
        uuid, pid, mem = [x.strip() for x in line.split(",")]
        user = subprocess.run(["ps", "-o", "user=", "-p", pid], capture_output=True, text=True).stdout.strip() or "?"
        if uuid in gpus:
            gpus[uuid]["procs"].append(dict(pid=int(pid), user=user, mem_mb=int(mem) if mem.isdigit() else mem))
    for p in (ROOT / "agents").glob("*.json"):
        a = read_json(p)
        if a and a.get("host") == host:
            for g in gpus.values():
                if str(g["gpu"]) in str(a.get("gpus", "")).split(","):
                    g["claimed_by"].append(dict(agent=a["id"], user=a["user"], exclusive=a.get("exclusive", False)))
    line = json.dumps(dict(ts=now(), host=host, gpus=sorted(gpus.values(), key=lambda g: g["gpu"])), ensure_ascii=False)
    fd = os.open(ROOT / "usage.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")
    last.touch()


def gpu_util():
    """{gpu index: utilization %} on this host, or {} without nvidia-smi."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {i.strip(): int(u) for i, u in (l.split(",") for l in out.splitlines() if "," in l)}


def warn_idle(a, p, util):
    """Leave a `warn` message in the entry's own inbox once its GPUs have all been under IDLE_UTIL % for IDLE_WARN s,
    and again every IDLE_WARN s while that lasts; the watching agent wakes on it."""
    gpus = [g for g in str(a.get("gpus", "")).split(",") if g in util]
    if not gpus:
        return
    if max(util[g] for g in gpus) >= IDLE_UTIL:
        if a.get("idle_since"):
            a.pop("idle_since"); a.pop("idle_warned", None)
            write_json(p, a)
        return
    t = now()
    if not a.get("idle_since"):
        a["idle_since"] = t
        write_json(p, a)
        return
    idle = t - a["idle_since"]
    if idle >= IDLE_WARN and t - a.get("idle_warned", 0) >= IDLE_WARN:
        text = (f"low utilization: gpus {','.join(gpus)} have been under {IDLE_UTIL}% for {idle // 60} min "
                f"(now {', '.join(f'gpu{g} {util[g]}%' for g in gpus)}). Release them, or tell your user why they are held.")
        send(argparse.Namespace(to=a["id"], sender="agent-bus", kind="warn", text=text, reply_to=None, gpus=None, minutes=None))
        a["idle_warned"] = t
        write_json(p, a)
        event("idle_warn", agent=a["id"], gpus=a.get("gpus"), idle_min=idle // 60)


def stale(a, host):
    """Why an entry no longer stands for anything, or None. Its pid not running here (when it was registered on this
    host); or past its expected end with no watcher heartbeat and no pid seen alive here. An entry whose pid lives
    on this host, or whose watcher beats, is never stale."""
    alive = None
    if a.get("pid") and a.get("host") == host:
        try:
            os.kill(int(a["pid"]), 0)
            alive = True
        except ProcessLookupError:
            return f"pid {a['pid']} not running on {host}"
        except PermissionError:  # another user's process: it exists
            alive = True
    if a.get("until") and a["until"] < now() and not alive and not attended(a):
        return f"expired {stamp(a['until'])} with no watcher" + ("" if a.get("pid") else " and no pid")
    return None


def sweep(args):
    """Run every 30 s by each user's own timer on the GPU host, for that user's entries there.

    First takes down, for every user, the entries that are stale (stale(): a dead pid on this host, or expired and
    unattended), archiving each with its reason and logging it. Every run touches .sweep/<user>@<host>, which a
    monitor can read to tell that this user's timer still fires.

    Also warns an entry's own agent (a `warn` message in its inbox) when its GPUs sit under IDLE_UTIL % for IDLE_WARN s.
    A request for GPUs stops the task when nobody can answer it: the entry's watcher is gone (no heartbeat for
    STALE seconds), or it is watched but the request sat unacknowledged for RESPOND seconds. Exempt: `keep`
    entries, entries without a pid (locks), and broadcasts to `all`. The stop is a SIGTERM to the launcher's wrapper, which stops its command and cleans up.
    """
    me, host = getpass.getuser(), socket.gethostname().split(".")[0]
    mark = ROOT / ".sweep" / f"{me}@{host}"
    mkdir(mark.parent)
    mark.touch()
    drop_ended_bookings()
    snapshot(host)
    util = gpu_util()
    for p in sorted((ROOT / "agents").glob("*.json")):
        a = read_json(p)
        if not a:
            continue
        why = stale(a, host)
        if why:
            if archive(p, a, f"stale: {why} (sweep)"):
                event("down", agent=a["id"], owner=a.get("user"), reason=f"stale: {why}", by="sweep")
            continue
        if a["user"] != me or a.get("host") != host:
            continue
        warn_idle(a, p, util)
        a = read_json(p) or a
        # only requests addressed to this entry: a broadcast to `all` never stops anything
        reqs = [m for m in unread_msgs(a) if m["kind"] == "request" and m.get("to") == a["id"]]
        if not reqs:
            continue
        watched = attended(a)
        oldest = now() - min(m["ts"] for m in reqs)
        why = "no watcher" if not watched else (f"no answer within {RESPOND}s" if oldest > RESPOND else None)
        policy = a.get("on_request", "agent")
        ids = sorted(m["id"] for m in reqs)
        if why is None:
            continue  # its agent is watching and still has time to decide
        if str(policy).startswith("keep"):
            if ids != a.get("sweep_noted"):
                a["sweep_noted"] = ids
                write_json(p, a)
                event("sweep", agent=a["id"], stopped=False, why=why, policy=policy, requesters=sorted({m["from"] for m in reqs}), msgs=ids)
            continue
        if not a.get("pid"):
            if ids != a.get("sweep_noted"):
                a["sweep_noted"] = ids
                write_json(p, a)
                event("sweep", agent=a["id"], stopped=False, why=why + "; entry has no pid to stop (a lock)", policy=policy,
                      requesters=sorted({m["from"] for m in reqs}), msgs=ids)
            continue
        try:
            os.kill(int(a["pid"]), signal.SIGTERM)  # the launcher's wrapper: it stops its command and cleans up
            stopped, text = True, f"{why}; task '{a.get('task')}' was stopped and gpus {a.get('gpus')} on {host} are free"
        except (ProcessLookupError, PermissionError) as e:
            stopped, text = False, f"{why}; could not stop pid {a['pid']}: {e!r}"
        for m in reqs if args.reply else []:  # install.sh's service passes --reply
            send(argparse.Namespace(to=m["from"], sender=a["id"], kind="reply", text=text, reply_to=m["id"], gpus=None, minutes=None))
        a["seen"] = sorted(set(a.get("seen", [])) | set(ids))
        a["sweep_noted"] = ids
        write_json(p, a)
        event("sweep", agent=a["id"], stopped=stopped, why=why, policy=policy, pid=a["pid"], gpus=a.get("gpus"),
              requesters=sorted({m["from"] for m in reqs}), msgs=ids, replied=args.reply, text=text)


def booking_dir():
    return ROOT / "bookings"


def bookings():
    out = []
    for p in sorted(booking_dir().glob("*.json")) if booking_dir().is_dir() else []:
        b = read_json(p)
        if b:
            out.append(b)
    return sorted(out, key=lambda b: (b["start"], b["id"]))


def parse_gpus(text):
    """'0-3', '1,3' or '0-3,6' -> [0, 1, 2, 3, 6]."""
    out = set()
    for part in str(text).replace(" ", "").split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        if not lo.isdigit() or (hi and not hi.isdigit()) or int(hi or lo) < int(lo) or int(hi or lo) >= NGPUS:
            raise ValueError(f"bad GPU list {text!r}: use indices 0-{NGPUS - 1} like 0-3 or 1,3")
        out.update(range(int(lo), int(hi or lo) + 1))
    if not out:
        raise ValueError("no GPUs given")
    return sorted(out)


def gpu_text(gpus):
    """[0, 1, 2, 3, 6] -> '0-3,6'."""
    runs, out = [], []
    for g in gpus:
        if runs and g == runs[-1][1] + 1:
            runs[-1][1] = g
        else:
            runs.append([g, g])
    for lo, hi in runs:
        out.append(str(lo) if lo == hi else f"{lo}-{hi}")
    return ",".join(out)


def parse_time(text, base=None):
    """Epoch seconds of 'now', 'HH:MM', 'today HH:MM', 'tomorrow HH:MM', 'MM-DD HH:MM', 'YYYY-MM-DD HH:MM' or ISO, in TZ.
    Second value: whether it was a bare clock time (no day given)."""
    t = text.strip().lower()
    today = datetime.fromtimestamp(base or now(), TZ).date()
    if t == "now":
        return now(), False
    m = re.fullmatch(r"(?:(today|tomorrow|tmr)\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if m and (m[3] or m[4]):
        h, mi = int(m[2]), int(m[3] or 0)
        if m[4]:
            h = h % 12 + (12 if m[4] == "pm" else 0)
        day = today + timedelta(days=1 if m[1] in ("tomorrow", "tmr") else 0)
        return int(datetime(day.year, day.month, day.day, h, mi, tzinfo=TZ).timestamp()), not m[1]
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})[ t](\d{1,2}):(\d{2})", t)
    if m:
        return int(datetime(today.year, int(m[1]), int(m[2]), int(m[3]), int(m[4]), tzinfo=TZ).timestamp()), False
    try:
        d = datetime.fromisoformat(text.strip().replace(" ", "T", 1) if " " in text.strip() else text.strip())
    except ValueError:
        raise ValueError(f"cannot read time {text!r}: use HH:MM, 'tomorrow 09:00', 'MM-DD HH:MM' or ISO") from None
    return int((d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()), False


def when(t):
    return datetime.fromtimestamp(t, TZ).strftime("%a %m-%d %H:%M")


def overlaps(b, others):
    return [o for o in others if o["id"] != b["id"] and o["start"] < b["end"] and b["start"] < o["end"]
            and set(o["gpus"]) & set(b["gpus"])]


def book(args):
    """Record that someone needs GPUs over a time window. Information only: nothing is stopped because of it."""
    try:
        gpus = parse_gpus(args.gpus)
        start, _ = parse_time(args.start)
        end, bare = parse_time(args.end)
        if bare:  # a bare HH:MM is on the start's day, and '--from 22:00 --to 01:00' means 01:00 the next day
            end = parse_time(args.end, base=start)[0]
            end += 86400 if end <= start else 0
    except ValueError as e:
        sys.exit(str(e))
    if end <= start:
        sys.exit(f"the booking ends ({when(end)}) before it starts ({when(start)})")
    me = getpass.getuser()
    bid = f"{me}-{datetime.fromtimestamp(start, TZ).strftime('%Y%m%d-%H%M')}-{secrets.token_hex(2)}"
    b = dict(id=bid, user=me, name=args.name or me, gpus=gpus, start=start, end=end, note=args.note or "", created=now())
    for o in overlaps(b, bookings()):
        print(f"warning: overlaps {o['id']} by {o['name']} ({o['user']}): gpus {gpu_text(o['gpus'])} "
              f"{when(o['start'])} - {when(o['end'])}" + (f" ({o['note']})" if o.get("note") else ""))
    write_json(booking_dir() / f"{bid}.json", b)
    event("book", booking=bid, gpus=gpu_text(gpus), start=start, end=end, name=b["name"], note=b["note"][:200])
    print(f"booked {bid}: gpus {gpu_text(gpus)} {when(start)} - {when(end)} {TZ.key}")


def unbook(args):
    p = booking_dir() / f"{args.id}.json"
    b = read_json(p)
    if not b:
        sys.exit(f"no booking {args.id}; `bus.py cal` lists them")
    if b["user"] != getpass.getuser():
        sys.exit(f"{args.id} belongs to {b['user']}; only its owner removes it")
    p.unlink(missing_ok=True)
    event("unbook", booking=args.id, reason="by owner")
    print(f"removed booking {args.id}")


def drop_ended_bookings():
    for b in bookings():
        if b["end"] < now():
            try:
                (booking_dir() / f"{b['id']}.json").unlink()
            except FileNotFoundError:  # another member's sweep got it first
                continue
            event("unbook", booking=b["id"], owner=b["user"], reason="ended", by="sweep")


def board_host():
    """The board's GPU host, from the `config` setup-board.sh writes (host=<name>), or None."""
    try:
        return dict(l.split("=", 1) for l in (ROOT / "config").read_text().split() if "=" in l).get("host")
    except OSError:
        return None


def entries_now():
    """Board entries still standing (not exited), with their GPU indices."""
    out = []
    for p in sorted((ROOT / "agents").glob("*.json")):
        a = read_json(p)
        if not a:
            continue
        st = state(a)
        try:
            gpus = parse_gpus(a.get("gpus")) if str(a.get("gpus") or "").strip() else []
        except ValueError:
            gpus = []
        if st != "exited":
            out.append(dict(id=a["id"], user=a["user"], host=a.get("host"), gpus=gpus, until=a.get("until"), state=st,
                            task=a.get("task", ""), exclusive=a.get("exclusive", False)))
    return out


def cal(args):
    """Per GPU, the board entries on it now and the bookings over the next --days days, as a grid (or --json)."""
    t0 = now()
    t1 = t0 + int(args.days * 86400)
    bs = [b for b in bookings() if b["end"] > t0 and b["start"] < t1]
    es = [e for e in entries_now() if e["gpus"] and e["host"] == (board_host() or e["host"])]
    if args.json:
        print(json.dumps(dict(tz=TZ.key, now=t0, until=t1, gpus=list(range(NGPUS)), bookings=bs, entries=es),
                         indent=1, ensure_ascii=False))
        return
    step = 3600 * max(1, math.ceil(args.days * 24 / 48))  # at most 48 cells across
    # cells start on the hour (or the step's multiple) at or before now
    first = datetime.fromtimestamp(t0, TZ).replace(minute=0, second=0, microsecond=0)
    first -= timedelta(hours=first.hour % (step // 3600))
    cells = [int(first.timestamp()) + i * step for i in range(math.ceil((t1 - first.timestamp()) / step))]
    keys = {b["id"]: chr(ord("A") + i) if i < 26 else "*" for i, b in enumerate(bs)}
    ekeys = {e["id"]: chr(ord("a") + i) if i < 26 else "+" for i, e in enumerate(es)}
    print(f"GPUs of {board_host() or 'the board'}, {TZ.key}; one column = {step // 3600} h from {when(cells[0])}; "
          "A-Z bookings, a-z board entries (until their expected end), . free")
    def put(line, at, text):
        line[at:at + len(text)] = list(text[:max(0, len(line) - at)])
    days, hours = [" "] * len(cells), [" "] * len(cells)
    label = "%a %m-%d" if 86400 // step >= 10 else "%a"  # a day must have room for its label
    width = len(datetime.now(TZ).strftime(label))
    mids = [k for k, c in enumerate(cells) if datetime.fromtimestamp(c, TZ).hour < step // 3600]
    if not mids or mids[0] > width:  # the first, partial day is labelled when its label fits before midnight
        put(days, 0, datetime.fromtimestamp(cells[0], TZ).strftime(label))
    for k, c in enumerate(cells):
        d = datetime.fromtimestamp(c, TZ)
        if k in mids:  # the cell that holds midnight starts a day
            put(days, k, d.strftime(label))
        if d.hour % 6 == 0 and step <= 2 * 3600:
            put(hours, k, f"{d.hour:02d}")
    print(" " * 6 + "".join(days).rstrip())
    if step <= 2 * 3600:
        print(" " * 6 + "".join(hours).rstrip())
    for g in range(NGPUS):
        row = []
        for c in cells:
            hit = [keys[b["id"]] for b in bs if g in b["gpus"] and b["start"] < c + step and c < b["end"]]
            here = [ekeys[e["id"]] for e in es if g in e["gpus"] and c < (e["until"] or t1) and c + step > t0]
            ch = hit + here
            row.append(ch[0] if len(ch) == 1 else ("#" if ch else "."))
        print(f"gpu{g}  " + "".join(row))
    print("(# = more than one)")
    for e in es:
        print(f"  {ekeys[e['id']]} now  gpus {gpu_text(e['gpus']):<7} {e['user']:<8} {e['state']:<10} "
              f"until {when(e['until']) if e['until'] else '-':<15} {e['id']}")
    for b in bs:
        print(f"  {keys[b['id']]} book gpus {gpu_text(b['gpus']):<7} {b['name']:<8} {when(b['start'])} - {when(b['end'])}"
              f"  {b.get('note', '')[:60]}  [{b['id']}]")
    if not bs:
        print("  no bookings in this window (`bus.py book --gpus 0-3 --from 22:00 --to 01:00 --note ...`)")


def log(args):
    """Read the audit trail: every up/down/send/ack/sweep, or with --usage the GPU snapshots."""
    path = ROOT / ("usage.jsonl" if args.usage else "log.jsonl")
    since = now() - int(args.hours * 3600)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        sys.exit(f"no {path.name} yet")
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e["ts"] < since or (args.agent and args.agent not in line):
            continue
        if args.usage:
            cells = []
            for g in e["gpus"]:
                who = ",".join(sorted({p["user"] for p in g["procs"]})) or "-"
                cells.append(f"gpu{g['gpu']} {g['util']:>3}% {g['mem_mb'] // 1024:>3}G {who}")
            print(f"{stamp(e['ts'])} {e['host']}  " + " | ".join(cells))
        else:
            rest = {k: v for k, v in e.items() if k not in ("ts", "user", "action")}
            print(f"{stamp(e['ts'])} {e['user']:<7} {e['action']:<6} " + " ".join(f"{k}={v}" for k, v in rest.items()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("up", help="register or update an agent (one per running task)")
    s.add_argument("id")
    s.add_argument("--gpus", help="e.g. 1,3")
    s.add_argument("--task")
    s.add_argument("--hours", type=float, help="expected remaining runtime")
    s.add_argument("--pid", type=int, help="process on --host that stops the task cleanly on SIGTERM")
    s.add_argument("--host", help="short hostname where the GPUs are (default: this one)")
    s.add_argument("--human", help="who to contact")
    s.add_argument("--on-request", help="'agent' (default: the owning agent decides each request), 'release' (stop and free on any request), or 'keep: <reason>'")
    s.add_argument("--session", help="the Claude Code session that watches this entry, for stop-hook (default: $CLAUDE_CODE_SESSION_ID)")
    s.add_argument("--exclusive", action="store_true", default=None, help="nobody else should put work on these GPUs (default: shared)")
    s.add_argument("--shared", dest="exclusive", action="store_false", help="undo --exclusive")
    s.set_defaults(f=up)
    s = sp.add_parser("down", help="unregister an agent (archived)")
    s.add_argument("id")
    s.add_argument("--reason", default="done")
    s.set_defaults(f=down)
    s = sp.add_parser("ls", help="every registered agent and its state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(f=ls)
    s = sp.add_parser("send", help="leave a message in an agent's inbox ('all' broadcasts)")
    s.add_argument("to")
    s.add_argument("text", help="'-' reads it from stdin")
    s.add_argument("--from", dest="sender", help="your agent id (default: your netID)")
    s.add_argument("--kind", default="msg", choices=["msg", "request", "reply", "warn"])
    s.add_argument("--reply-to")
    s.add_argument("--gpus", help="for a request: how many or which")
    s.add_argument("--minutes", type=int, help="for a request: for how long")
    s.set_defaults(f=send)
    s = sp.add_parser("inbox", help="unread messages for an agent")
    s.add_argument("id")
    s.add_argument("--all", action="store_true")
    s.set_defaults(f=inbox)
    s = sp.add_parser("ack", help="mark messages read")
    s.add_argument("id")
    s.add_argument("msg", nargs="+")
    s.set_defaults(f=ack)
    s = sp.add_parser("watch", help="heartbeat until messages arrive, print the unacked ones, exit (one watch per entry)")
    s.add_argument("id")
    s.add_argument("--interval", type=int, default=5)
    s.add_argument("--follow", action="store_true", help="keep printing new messages instead of exiting on the first")
    s.add_argument("--exit-on-eof", action="store_true", help="--follow, and stop when stdin closes (watch.sh runs it over ssh)")
    s.set_defaults(f=watch)
    s = sp.add_parser("book", help="say when you need GPUs (information only: nothing is stopped because of a booking)")
    s.add_argument("--gpus", required=True, help="e.g. 0-3 or 1,3")
    s.add_argument("--from", dest="start", required=True, help="HH:MM, 'tomorrow 09:00', 'MM-DD HH:MM' or ISO (local ET)")
    s.add_argument("--to", dest="end", required=True, help="the same forms; a bare HH:MM before --from is the next day")
    s.add_argument("--note", default="", help="what it is for")
    s.add_argument("--as", dest="name", help="the name to show (default: your netID)")
    s.set_defaults(f=book)
    s = sp.add_parser("unbook", help="remove a booking of yours")
    s.add_argument("id")
    s.set_defaults(f=unbook)
    s = sp.add_parser("cal", help="per GPU: who is on it now and the bookings ahead")
    s.add_argument("--days", type=float, default=2)
    s.add_argument("--json", action="store_true")
    s.set_defaults(f=cal)
    s = sp.add_parser("stop-hook", help="Claude Code Stop hook: block the turn while an entry of this session has no watch")
    s.set_defaults(f=stop_hook)
    s = sp.add_parser("log", help="read the audit trail (log.jsonl) or, with --usage, the GPU snapshots (usage.jsonl)")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--agent", help="only lines mentioning this agent id")
    s.add_argument("--usage", action="store_true")
    s.set_defaults(f=log)
    s = sp.add_parser("sweep", help="take down stale entries; stop your tasks on this host whose requests nobody answers; GPU snapshots (for a timer)")
    s.add_argument("--reply", action="store_true", help="also post the fixed reply to the requester")
    s.set_defaults(f=sweep)
    a = ap.parse_args()
    os.umask(0o002)
    a.f(a)


if __name__ == "__main__":
    main()
