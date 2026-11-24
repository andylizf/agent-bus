"""`bus`: the agent-bus v2 command line. `bus <command> -h` for each command's options."""
import argparse, hashlib, json, os, secrets, shlex, shutil, subprocess, sys, time

from . import bookings as bk
from . import core, runner
from .core import Refused, now

USAGE = """bus: coordinate a shared GPU host through a board directory (agent-bus v2)

  runs:      run, exec, release, state, runs, reconcile, gc --mine
  reading:   ls, cal, watch
  bookings:  book, unbook, object
  messages:  send, reply, mailbox
  install:   doctor, upgrade, adopt --all, stop-hook, page
"""


def out(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True, default=str))


def when(t):
    return time.strftime("%a %m-%d %H:%M", time.localtime(t)) if isinstance(t, (int, float)) else "-"


def board():
    return core.Board.from_conf()


# ------------------------------------------------------------------------------------------------ reading

def view(b, days=2, all_bookings=False):
    t = now()
    entries = bk.entries_with_occupancy(b)
    rows = bk.evaluate(b, t, entries=entries)
    rows = [r for r in rows if all_bookings or (r.get("state") != "unpublished" and r["end"] > t
                                                and r["start"] < t + days * 86400)]
    return t, [core.clean(e) for e in entries], rows


def cmd_ls(a):
    with board() as b:
        t, entries, rows = view(b, 1)
        if a.json:
            return out(dict(board=dict(path=b.path, identity="ok" if b.ok else "changed"), now=t, entries=entries,
                            bookings=rows, warnings=b.warnings))
        if not b.ok:
            print(f"board identity changed at {b.path}: shown as read; nothing will act on it")
        for e in entries:
            occ = "" if e["occupancy"] == "live" else "  occupancy unknown"
            print(f"{e['id']:<44} {e['user']:<8} gpus {core.gpu_text(e['gpus']):<7} {e['class']:<9} {e['state']:<13} "
                  f"until {when(e['until'])}  {e['kind']}  {e.get('note', '')}{occ}")
        if not entries:
            print("no runs on the board")
        for r in rows:
            print(f"  booking {r['id']}  {r['user']}  gpus {core.gpu_text(r['gpus'])}  {when(r['start'])} - "
                  f"{when(r['end'])}  {r['state']}{'  ' + r['why'] if r.get('why') else ''}")


def cmd_cal(a):
    with board() as b:
        t, entries, rows = view(b, a.days, a.all)
        if a.json:
            return out(dict(now=t, tz=time.strftime("%Z"), gpus=list(range(b.ngpus)), bookings=rows, entries=entries,
                            identity="ok" if b.ok else "changed"))
        for g in range(b.ngpus):
            here = [e for e in entries if g in e["gpus"]]
            ahead = [r for r in rows if g in r["gpus"]]
            line = ", ".join(f"{e['user']} {e['class']} until {when(e['until'])}" for e in here) or "free"
            print(f"gpu{g}  now: {line}")
            for r in ahead:
                print(f"       {when(r['start'])} - {when(r['end'])}  {r['user']}  {r['state']}  [{r['id']}]")


# ------------------------------------------------------------------------------------------------ bookings

def cmd_book(a):
    with board() as b:
        gpus = core.parse_gpus(a.gpus, b.ngpus)
        res = bk.publish_booking(b, gpus, bk.parse_when(a.start), bk.parse_when(a.end),
                                 bk.parse_duration(a.objection) if a.objection else None, a.kind, a.note or "",
                                 a.source, a.ref, a.slug, a.revise)
    if a.json:
        return out(res)
    print(f"booked {res['id']} (receipt {res['slot']}): gpus {core.gpu_text(gpus)} {when(res['start'])} - "
          f"{when(res['end'])}; {res['state']}, objections until {when(res['objection_close'])}")
    for o in res["overlaps"]:
        print(f"note: overlaps {o['id']} ({o['user']}, {o['state']})")


def cmd_unbook(a):
    core.component(a.id, "booking id")
    with board() as b:
        bk.unbook(b, a.id)
    print(f"removed booking {a.id}")


def cmd_object(a):
    with board() as b:
        if a.withdraw:
            bk.withdraw_objection(b, a.withdraw)
            return print(f"withdrew objection {a.withdraw}")
        if not a.booking:
            raise Refused("name the booking: bus object <booking-id> --reason <text>")
        res = bk.publish_objection(b, a.booking, a.reason)
    if a.json:
        return out(res)
    print(f"objection {res['id']} to {res['booking']} published (receipt {res['slot']})")


# ------------------------------------------------------------------------------------------------ messages

def own_mailbox(b, name="mail"):
    box = core.component(f"{core.USER}-{name}", "mailbox")
    fd = core.mkdir_own(b.wdir("inbox"), box, 0o3775)
    if fd is None:
        raise Refused(f"inbox/{box} exists and is not yours")
    os.close(fd)
    return box


def cmd_mailbox(a):
    with board() as b:
        print(own_mailbox(b, a.name))


def cmd_send(a):
    with board() as b:
        b.writable()
        to_run = a.to_run
        if a.kind == "request" and not to_run:
            e = next((e for e in b.entries() if e["id"] == a.to), None)
            if e is None:
                raise Refused(f"no run {a.to} on the board to request from (pass --to-run for a mailbox)")
            to_run = e["run"]
        reply_to = a.reply_to or own_mailbox(b)
        if b.inbox_owner(reply_to) != core.USER:
            raise Refused(f"--reply-to {reply_to} is not a mailbox of yours (`bus mailbox <name>`)")
        core.component(a.to, "recipient")
        fd = b.dir("inbox", a.to)
        if fd is None:
            raise Refused(f"no inbox {a.to} on the board")
        mid = core.new_id(f"msg-{secrets.token_hex(3)}")
        m = dict(id=mid, sender=core.USER, to=a.to, to_run=to_run, kind=a.kind, request_id=secrets.token_hex(16),
                 gpus=a.gpus, minutes=a.minutes, reply_to=reply_to, note=a.note or "", schema=core.SCHEMA,
                 created=now())
        if not core.check_message(m):
            raise Refused("message does not validate (note at most 200 characters)")
        core.publish(fd, f"{mid}.json", core.dumps(m))
        b.log("send", msg=mid, to=a.to, kind=a.kind)
    if a.json:
        return out(dict(id=mid, request_id=m["request_id"], reply_to=reply_to, to_run=to_run))
    print(f"sent {mid} to {a.to}; replies arrive in {reply_to} (`bus watch {reply_to}`)")


def my_boxes(b):
    fd = b.dir("inbox")
    names, _ = core.scan(fd) if fd is not None else ([], 0)
    return [n for n in names or [] if n.startswith(core.USER + "-") and b.inbox_owner(n) == core.USER]


def cmd_reply(a):
    with board() as b:
        b.writable()
        for box in my_boxes(b):
            m = next((m for m in b.messages(box) if m["id"] == a.msg), None)
            if m:
                break
        else:
            raise Refused(f"no message {a.msg} in your inboxes")
        rt = m.get("reply_to")
        if not (isinstance(rt, str) and core.owned_id(rt, m["sender"])) or b.inbox_owner(rt) != m["sender"]:
            raise Refused("that message names no mailbox of its sender to reply to")   # checked before any path use
        if a.answer == "no" and not (a.reason or "").strip():
            raise Refused("a no needs a reason (--reason)")
        mid = core.new_id(f"re-{secrets.token_hex(3)}")
        r = dict(id=mid, sender=core.USER, to=m["reply_to"], kind="reply", request_id=m.get("request_id"),
                 answer=a.answer, reason=a.reason, note=a.note or "", reply_to=None, schema=core.SCHEMA, created=now())
        core.publish(b.dir("inbox", m["reply_to"]), f"{mid}.json", core.dumps(r))
    print(f"replied {a.answer} to {a.msg}")


def fmt_msg(m):
    extra = " ".join(f"{k}={m[k]}" for k in ("gpus", "minutes", "answer", "reason", "to_run") if m.get(k))
    return f"MSG {m['id']} from={m['sender']} kind={m['kind']} {extra}: {m.get('note', '')}".rstrip()


def cmd_watch(a):
    """Print new messages to an entry or mailbox, and the run's notices; writes nothing on the board."""
    core.component(a.id)
    wdir = core.private_dir("watch")
    lk = runner.Locked(os.path.join(wdir, f"{a.id}.lock"))
    if not lk.acquire():
        print(f"WATCH {a.id} already watched; this one exits", flush=True)
        return 3
    printed_path = os.path.join(wdir, f"{a.id}.printed.json")
    printed = set(core.read_private(printed_path) or [])
    try:
        while True:
            new = []
            with board() as b:
                for m in b.messages(a.id):
                    if m["id"] not in printed:
                        new.append(fmt_msg(m))
                        printed.add(m["id"])
            s = core.read_private(runner.state_path(a.id)) or {}
            for n in s.get("notices", []):
                key = f"n{n.get('time')}-{n.get('event')}"
                if key not in printed:
                    printed.add(key)
                    new.append(f"NOTICE {a.id} {n.get('event')} " + json.dumps(
                        {k: v for k, v in n.items() if k not in ('event', 'id')}, ensure_ascii=False))
            for line in new:
                print(line, flush=True)
            core.write_private(printed_path, sorted(printed))
            if (new and a.once) or s.get("state") == "ended":
                if s.get("state") == "ended":
                    print(f"WATCH {a.id} ended: {s.get('end_reason')}", flush=True)
                return 0
            time.sleep(a.interval)
    finally:
        lk.release()


def cmd_stop_hook(a):
    """Claude Code Stop hook: block the turn's end while a run this session started has no --on-message hook and no
    `bus watch` running for it."""
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return
    sid = hook.get("session_id")
    if not sid:
        return
    need = []
    for r in runner.cmd_runs():
        s = core.read_private(runner.state_path(r["id"])) or {}
        if s.get("session") != sid or s.get("state") not in runner.NONTERMINAL or s.get("on_message"):
            continue
        if runner.lock_free(os.path.join(core.private_dir("watch"), f"{r['id']}.lock")):
            need.append(r["id"])
    counter = os.path.join(core.private_dir("stop-hook"), f"{sid}.json")
    blocks = (core.read_private(counter) or {}).get("blocks", 0) if hook.get("stop_hook_active") else 0
    if not need or blocks >= 3:
        if not need and os.path.exists(counter):
            os.unlink(counter)
        return
    core.write_private(counter, dict(blocks=blocks + 1, runs=need))
    print(json.dumps(dict(decision="block", reason=(
        f"agent-bus: {', '.join(need)} {'runs' if len(need) == 1 else 'run'} with no --on-message hook and no "
        f"`bus watch` for it, so this session would not learn of a release. Start `bus watch <id>` with the Bash "
        f"tool, run_in_background true, for: {' '.join(need)}; then end the turn."))))


# ------------------------------------------------------------------------------------------------ runs

def cmd_release(a):
    r = runner.cmd_release(a.id, a.reason, a.wait)
    if a.json:
        out(r)
    else:
        print(f"{a.id}: {'released' if r['released'] else r['state']}" +
              (f"; remains: {'; '.join(r['remains'])}" if r["remains"] else ""))
    return 0 if r["released"] or not a.wait else 1


def cmd_gc(a):
    if not a.mine:
        raise Refused("gc acts on your own state only: bus gc --mine")
    res = dict(orphans=[], archived=[])
    states = {r["id"]: r for r in runner.cmd_runs()}
    for rid, r in states.items():
        if r["state"] in runner.NONTERMINAL and r.get("host") == core.HOST \
                and runner.lock_free(os.path.join(runner.runs_dir(), f"{rid}.lock")):
            res["orphans"].append(dict(id=rid, state=r["state"], mode=r.get("mode"),
                                       note="no supervisor holds it: `bus adopt --all` (systemd mode) or stop its "
                                            "processes by hand (fallback mode)"))
    with board() as b:
        if b.ok:
            for e in b.entries():
                if e["user"] != core.USER:
                    continue
                st = states.get(e["id"])
                if st is None or st["state"] == "ended":
                    afd, gfd = b.dir("agents"), b.dir("archive")
                    try:
                        os.rename(f"{e['id']}.json", f"{e['id']}-{now()}.json", src_dir_fd=afd, dst_dir_fd=gfd)
                        res["archived"].append(e["id"])
                        hb = b.dir("agents", ".heartbeat")
                        if hb is not None:
                            core.unlink_own(hb, e["id"])
                    except OSError:
                        pass
    if a.json:
        return out(res)
    for o in res["orphans"]:
        print(f"orphan {o['id']} ({o['state']}, {o['mode']}): {o['note']}")
    for x in res["archived"]:
        print(f"archived {x} (no live private run)")


def cmd_doctor(a):
    checks = []

    def check(name, ok, detail=""):
        checks.append(dict(check=name, ok=bool(ok), detail=detail))
    try:
        lin = subprocess.run(["loginctl", "show-user", core.USER, "-p", "Linger", "--value"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        lin = repr(e)
    check("linger", lin == "yes", lin)
    um = runner.user_manager_ok()
    check("user manager", um, "systemd mode" if um else "fallback mode: no adoption, no hot update, no exec")
    check("cgroup2", os.path.exists("/sys/fs/cgroup/cgroup.controllers"))
    if um:
        sl = f"{core.PREFIX()}-doctor_{secrets.token_hex(3)}.slice"
        try:
            r = subprocess.run(["systemd-run", "--user", "--quiet", "--scope", f"--slice={sl}", "--", "cat",
                                "/proc/self/cgroup"], capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0 and sl in r.stdout
            detail = r.stdout.strip()[-160:] or r.stderr.strip()[-160:]
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, detail = False, repr(e)
        runner.sysctl("stop", sl)
        check("slice test", ok, detail)
    else:
        check("slice test", False, "skipped: no user manager")
    conf = core.load_conf()
    try:
        with core.Board.from_conf(conf) as b:
            check("board pin", b.ok and b.pin is not None, f"{b.path} inode {b.ino} fsid {b.fsid}"
                  + ("" if b.ok else f" (pinned {conf.get('inode')} {conf.get('fsid')})"))
            check("board host", not b.host or b.host in (core.HOST, os.uname().nodename),
                  f"board host {b.host}, this host {core.HOST}")
    except (OSError, Refused) as e:
        check("board pin", False, str(e))
    rc, o, _ = runner.sysctl("is-enabled", "bus-adopt.timer")
    check("adopt timer", o.strip() == "enabled", o.strip() or "not installed")
    check("build", os.path.exists(core.BIN()), f"{core.BIN()} -> {runner.installed_build()}; this {runner.build_id()}")
    if a.json:
        out(dict(ok=all(c["ok"] for c in checks), checks=checks))
    else:
        for c in checks:
            print(f"{'ok  ' if c['ok'] else 'FAIL'} {c['check']}: {c['detail']}")
    return 0 if all(c["ok"] for c in checks) else 1


def copy_build(src):
    """Copy a source tree into builds/<id> (id: the git commit's first 12, else a content hash) and return it."""
    src = os.path.realpath(src)
    if not (os.path.isfile(os.path.join(src, "bin", "bus")) and os.path.isdir(os.path.join(src, "lib", "agentbus"))):
        raise Refused(f"{src} is not an agent-bus build (bin/bus and lib/agentbus/)")
    builds = core.BUILDS()
    if os.path.dirname(src) == os.path.realpath(builds):
        return src
    try:
        bid = subprocess.run(["git", "-C", src, "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", src, "status", "--porcelain", "--", "bin", "lib"], capture_output=True,
                               text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        bid, dirty = "", ""
    h = hashlib.sha256()
    for d, _, files in sorted(os.walk(os.path.join(src, "lib"))):
        for f in sorted(files):
            if f.endswith(".py"):
                h.update(open(os.path.join(d, f), "rb").read())
    h.update(open(os.path.join(src, "bin", "bus"), "rb").read())
    if not bid or dirty:
        bid = (bid + "-" if bid else "") + "w" + h.hexdigest()[:8]
    dest = os.path.join(builds, bid)
    if not os.path.isdir(dest):
        os.makedirs(builds, exist_ok=True)
        tmp = dest + f".tmp{secrets.token_hex(3)}"
        os.makedirs(tmp)
        for part in ("bin", "lib"):
            shutil.copytree(os.path.join(src, part), os.path.join(tmp, part),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for d, dirs, files in os.walk(tmp):
            for n in dirs + files:
                p = os.path.join(d, n)
                os.chmod(p, os.stat(p).st_mode & ~0o022)
        os.chmod(tmp, 0o755)
        os.rename(tmp, dest)
    return dest


def flip(build):
    link = core.BIN()
    os.makedirs(os.path.dirname(link), exist_ok=True)
    tmp = link + f".new{secrets.token_hex(3)}"
    os.symlink(os.path.join(build, "bin", "bus"), tmp)
    os.replace(tmp, link)


def cmd_upgrade(a):
    build = copy_build(a.build)
    before = runner.installed_build()
    flip(build)
    live = [r["id"] for r in runner.cmd_runs() if r["state"] in runner.NONTERMINAL and r.get("host") == core.HOST]
    res = dict(previous=before, installed=os.path.basename(build), supervisors=live)
    if a.json:
        return out(res)
    print(f"installed {res['installed']} (was {before}); {len(live)} supervisor(s) exec it at their next pass")


def cmd_setup_board(a):
    from . import setup
    readme = open(a.readme, "rb").read() if a.readme else None
    if a.add_blocks is not None:
        r = setup.setup_board(a.board, add_blocks=a.add_blocks, group=a.group)
        print(f"seq blocks now {r['blocks']}")
        return
    if not a.target:
        raise Refused("usage: setup-board.sh <board-dir> <gpu-host> [--group G] [--gpus N] [--blocks N]")
    r = setup.setup_board(a.board, host=a.target, group=a.group, gpus=a.gpus, blocks=a.blocks, readme=readme)
    print(f"v2 board ready at {r['root']} for host {a.target} ({a.blocks} receipt blocks)")
    print(f"each member: install bus privately (bin/install.sh --board {r['root']}) and run `bus doctor`")


def cmd_install(a):
    """`bus _install --board DIR` (install.sh calls it): copy the build, flip the entry point, pin the board by inode
    and filesystem id in ~/.config/agent-bus/board.conf."""
    build = copy_build(a.src)
    flip(build)
    conf = core.load_conf()
    b = core.Board(os.path.abspath(a.board))
    conf.update(path=b.path, inode=str(b.ino), fsid=str(b.fsid), host=b.host or "")
    b.close()
    core.save_conf(conf)
    out(dict(build=os.path.basename(build), bin=core.BIN(), conf=conf))


# ------------------------------------------------------------------------------------------------ parser

def parser():
    ap = argparse.ArgumentParser(prog="bus", description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    def p(name, f, help=None):
        s = sp.add_parser(name, help=help)
        s.set_defaults(f=f)
        return s

    s = p("run", None, "start a job on the board's host (backfill, or --scheduled <booking>)")
    s.add_argument("--gpus", required=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--scheduled", metavar="BOOKING")
    s.add_argument("--hours", type=float)
    s.add_argument("--exclusive", action="store_true")
    s.add_argument("--kind", default="other")
    s.add_argument("--note", default="")
    s.add_argument("--slug")
    s.add_argument("--cwd")
    s.add_argument("--log")
    s.add_argument("--info-file")
    s.add_argument("--on-message")
    s.add_argument("--mem-max", type=int)
    s.add_argument("--checkpoint-dir")
    s.add_argument("--stop-cost-min", type=int)
    s.add_argument("--detach", action="store_true")
    s.add_argument("command", nargs="?")
    s.set_defaults(f=lambda a: runner.cmd_run(a))
    s = p("exec", lambda a: runner.cmd_exec(a.id, a.run, a.argv, a.cwd), "run a command inside a run's slice")
    s.add_argument("id")
    s.add_argument("--run", required=True)
    s.add_argument("--cwd")
    s.add_argument("argv", nargs="*")
    s = p("release", cmd_release, "release one of your runs (verified)")
    s.add_argument("id")
    s.add_argument("--reason")
    s.add_argument("--wait", type=float)
    s.add_argument("--json", action="store_true")
    s = p("state", lambda a: out(runner.cmd_state(a.id)), "a run's private state")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")
    s = p("runs", lambda a: out(runner.cmd_runs()), "your runs' private states")
    s.add_argument("--json", action="store_true")
    s = p("reconcile", lambda a: out(runner.cancel_exec(a.id, a.cancel_exec) if a.cancel_exec
                                     else runner.cmd_reconcile(a.id, a.confirm)),
          "list or confirm processes a release cannot attribute; clear an unidentifiable exec")
    s.add_argument("id")
    s.add_argument("--confirm", nargs="*", type=int, default=[])
    s.add_argument("--cancel-exec", metavar="EXEC_ID")
    s = p("supervise", lambda a: runner.Supervisor(a.id, adopt=a.adopt).run(), "the per-run supervisor (internal)")
    s.add_argument("id")
    s.add_argument("--adopt", action="store_true")
    s = p("adopt", lambda a: out(runner.cmd_adopt_all()), "start a supervisor for every unsupervised run here")
    s.add_argument("--all", action="store_true", required=True)
    s = p("_exec-guard", lambda a: runner.exec_guard(a.id, a.eid, a.run, a.argv))
    s.add_argument("id")
    s.add_argument("eid")
    s.add_argument("run")
    s.add_argument("argv", nargs="*")
    s = p("_job", lambda a: runner.job_main(a.id))
    s.add_argument("id")
    s = p("gc", cmd_gc, "your orphaned runs and stale entries")
    s.add_argument("--mine", action="store_true")
    s.add_argument("--json", action="store_true")
    s = p("ls", cmd_ls, "runs on the board and bookings of the next day")
    s.add_argument("--json", action="store_true")
    s = p("cal", cmd_cal, "per GPU: who is on it now and the bookings ahead")
    s.add_argument("--days", type=float, default=2)
    s.add_argument("--json", action="store_true")
    s.add_argument("--all", action="store_true", help="every booking file, unpublished ones too")
    s = p("book", cmd_book, "publish a booking (a notice with an objection window)")
    s.add_argument("--gpus", required=True)
    s.add_argument("--from", dest="start", required=True)
    s.add_argument("--to", dest="end", required=True)
    s.add_argument("--objection")
    s.add_argument("--kind", default="other")
    s.add_argument("--note", default="")
    s.add_argument("--source", default="cli", choices=["cli", "gpupool", "request"])
    s.add_argument("--ref")
    s.add_argument("--slug")
    s.add_argument("--revise", metavar="BOOKING", help="publish a new revision of your own booking")
    s.add_argument("--json", action="store_true")
    s = p("unbook", cmd_unbook, "withdraw your booking")
    s.add_argument("id")
    s = p("object", cmd_object, "object to a booking, with a reason, during its objection window")
    s.add_argument("booking", nargs="?")
    s.add_argument("--reason")
    s.add_argument("--withdraw", metavar="OBJECTION")
    s.add_argument("--json", action="store_true")
    s = p("send", cmd_send, "leave a message in a run's inbox or a mailbox")
    s.add_argument("to")
    s.add_argument("--kind", default="request", choices=["request", "notice"])
    s.add_argument("--gpus", type=int)
    s.add_argument("--minutes", type=int)
    s.add_argument("--to-run")
    s.add_argument("--note", default="")
    s.add_argument("--reply-to")
    s.add_argument("--json", action="store_true")
    s = p("reply", cmd_reply, "answer a message in one of your inboxes")
    s.add_argument("msg")
    s.add_argument("--answer", required=True, choices=["yes", "no"])
    s.add_argument("--reason")
    s.add_argument("--note", default="")
    s = p("mailbox", cmd_mailbox, "create your mailbox inbox/<user>-<name>")
    s.add_argument("name")
    s = p("watch", cmd_watch, "print new messages and notices for a run or mailbox; writes nothing on the board")
    s.add_argument("id")
    s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=float, default=5)
    p("stop-hook", cmd_stop_hook, "Claude Code Stop hook")
    s = p("doctor", cmd_doctor, "check linger, user manager, cgroup2, a test slice and the board pin")
    s.add_argument("--json", action="store_true")
    s = p("upgrade", cmd_upgrade, "install a build and flip ~/.local/bin/bus; supervisors exec it")
    s.add_argument("--build", required=True)
    s.add_argument("--json", action="store_true")
    s = p("_setup-board", cmd_setup_board)
    s.add_argument("board")
    s.add_argument("target", nargs="?", default=None)
    s.add_argument("--add-blocks", type=int)
    s.add_argument("--group")
    s.add_argument("--gpus", type=int, default=8)
    s.add_argument("--blocks", type=int, default=1000)
    s.add_argument("--readme")
    s = p("_install", cmd_install)
    s.add_argument("--src", required=True)
    s.add_argument("--board", required=True)
    s = p("page", None, "serve the read-only board page")
    s.add_argument("--port", type=int, default=8300)
    s.add_argument("--host", default="127.0.0.1")
    s.set_defaults(f=cmd_page)
    return ap


def cmd_page(a):
    from . import page
    srv, _ = page.serve(a.port, a.host)
    print(f"serving the read-only board page on http://{a.host}:{a.port}/", flush=True)
    srv.serve_forever()


def main(argv=None):
    os.umask(0o022)
    argv = list(sys.argv[1:] if argv is None else argv)
    tail = None
    if argv[:1] in (["run"], ["exec"], ["_exec-guard"]) and "--" in argv:      # bus run [options] -- '<command>'
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    a = parser().parse_args(argv)
    if a.cmd == "run":
        if tail is None and not a.command:
            print("bus: give the command after --", file=sys.stderr)
            return 2
        a.command = (tail[0] if len(tail) == 1 else shlex.join(tail)) if tail is not None else a.command
    if a.cmd in ("exec", "_exec-guard"):
        a.argv = tail if tail is not None else a.argv
        if not a.argv:
            print("bus: give the command after --", file=sys.stderr)
            return 2
    try:
        rc = a.f(a)
    except Refused as e:
        print(f"bus: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    return rc if isinstance(rc, int) else 0
