#!/usr/bin/env python3
"""agent-bus: a shared directory where agents that started work on a shared GPU box say what they hold and take messages.

No server. Each running task registers itself as an agent (agents/<id>.json: who, which GPUs, until when, what
happens on a request for its GPUs); anyone, person or agent, drops a message file into inbox/<id>/; the agent
that owns the task keeps `bus.py watch <id>` running, which refreshes its heartbeat and prints each new message
as one line, so the agent wakes up and answers with `bus.py send`. The watch polls (every 5 s): GPFS does not
deliver inotify events for files written from another node. Every write is a new file or an atomic rename, so
concurrent writers never corrupt each other. Python 3.9, standard library only.
"""
import argparse, getpass, json, os, secrets, select, signal, socket, subprocess, sys, time
from pathlib import Path

ROOT = Path(os.environ.get("AGENT_BUS_ROOT") or Path(__file__).resolve().parent.parent)  # the board is the directory above bin/
STALE = 60           # seconds without a heartbeat before an agent counts as unattended (watch beats every 5 s)
RESPOND = 300        # seconds a watched agent has to act on a request before it counts as unresponsive
SNAPSHOT_EVERY = 120 # seconds between GPU usage snapshots written by the sweep
IDLE_UTIL = 5        # percent: an entry whose GPUs are all below this is idle
IDLE_WARN = int(os.environ.get("AGENT_BUS_IDLE_WARN", 1800))  # seconds idle before the owner's agent is warned, and between warnings
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
             started=old.get("started", now()), heartbeat=now(), seen=old.get("seen", []))
    write_json(agent_path(aid), a)
    beat(aid)
    mkdir(ROOT / "inbox" / aid)
    event("up", agent=aid, gpus=a["gpus"], host=a["host"], task=a["task"], until=a["until"], on_request=a["on_request"],
          exclusive=a["exclusive"], pid=a["pid"])
    print(f"agent {aid} up: {a['host']} gpus={a['gpus']} until={stamp(a['until']) if a['until'] else '-'} "
          f"on_request={a['on_request']} {'exclusive' if a['exclusive'] else 'shared'}\nwatch: python3 {ROOT}/bin/bus.py watch {aid}")


def down(args):
    p = agent_path(args.id)
    a = read_json(p)
    if not a:
        sys.exit(f"no agent {args.id}")
    if a["user"] != getpass.getuser():
        sys.exit(f"{args.id} belongs to {a['user']}; only its owner takes it down")
    mkdir(ROOT / "archive")
    a.update(down_at=now(), down_reason=args.reason)
    write_json(p, a)
    os.replace(p, ROOT / "archive" / f"{args.id}-{now()}.json")
    hb_path(args.id).unlink(missing_ok=True)
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


def watch(args):
    """Heartbeat + one line per new message, until the agent is taken down. Run it where your agent sees stdout."""
    p = agent_path(args.id)
    a = read_json(p)
    if not a:
        sys.exit(f"no agent {args.id}; `bus.py up` first")
    if a["user"] != getpass.getuser():
        sys.exit(f"{args.id} belongs to {a['user']}")
    print(f"WATCH {args.id} up; {len(unread_msgs(a))} unread", flush=True)
    for m in unread_msgs(a):  # anything that arrived while nobody watched
        print(fmt(m), flush=True)
    printed = {m["id"] for m in unread_msgs(a)}
    said = None
    while True:
        a = read_json(p)
        if not a:
            print(f"WATCH {args.id} down: agent file removed", flush=True)
            return
        st = state(a)
        if st in ("exited", "expired") and st != said:
            said = st
            print(f"WATCH {args.id} {st}: take it down with `bus.py down {args.id}` or extend with `bus.py up {args.id} --hours N`", flush=True)
        for m in unread_msgs(a):
            if m["id"] not in printed:
                print(fmt(m), flush=True)
                printed.add(m["id"])
        beat(args.id)
        if args.exit_on_eof:
            r, _, _ = select.select([sys.stdin], [], [], args.interval)
            if r and not sys.stdin.read(1):
                return
        else:
            time.sleep(args.interval)


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


def sweep(args):
    """Run every 30 s by each user's own timer on the GPU host, for that user's entries there.

    Also warns an entry's own agent (a `warn` message in its inbox) when its GPUs sit under IDLE_UTIL % for IDLE_WARN s.
    A request for GPUs stops the task when nobody can answer it: the entry's watcher is gone (no heartbeat for
    STALE seconds), or it is watched but the request sat unacknowledged for RESPOND seconds. Exempt: `keep`
    entries, entries without a pid (locks), and broadcasts to `all`. The stop is a SIGTERM to the launcher's wrapper, which stops its command and cleans up.
    """
    me, host = getpass.getuser(), socket.gethostname().split(".")[0]
    snapshot(host)
    util = gpu_util()
    for p in sorted((ROOT / "agents").glob("*.json")):
        a = read_json(p)
        if not a or a["user"] != me or a.get("host") != host:
            continue
        if state(a) == "exited":
            mkdir(ROOT / "archive")
            a.update(down_at=now(), down_reason="process exited (sweep)")
            write_json(p, a)
            os.replace(p, ROOT / "archive" / f"{a['id']}-{now()}.json")
            hb_path(a["id"]).unlink(missing_ok=True)
            event("down", agent=a["id"], reason="process exited (sweep)")
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
    s = sp.add_parser("watch", help="heartbeat and print new messages, one line each")
    s.add_argument("id")
    s.add_argument("--interval", type=int, default=5)
    s.add_argument("--exit-on-eof", action="store_true", help="stop when stdin closes (watch.sh runs it over ssh)")
    s.set_defaults(f=watch)
    s = sp.add_parser("log", help="read the audit trail (log.jsonl) or, with --usage, the GPU snapshots (usage.jsonl)")
    s.add_argument("--hours", type=float, default=24)
    s.add_argument("--agent", help="only lines mentioning this agent id")
    s.add_argument("--usage", action="store_true")
    s.set_defaults(f=log)
    s = sp.add_parser("sweep", help="stop your tasks on this host whose requests nobody answers; GPU snapshots (for a timer)")
    s.add_argument("--reply", action="store_true", help="also post the fixed reply to the requester")
    s.set_defaults(f=sweep)
    a = ap.parse_args()
    os.umask(0o002)
    a.f(a)


if __name__ == "__main__":
    main()
