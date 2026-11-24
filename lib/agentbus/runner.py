"""The job as systemd units, its supervisor, admission, verified release, adoption and hot update (design 3.3-3.8).

  run:        main job = transient service <prefix>-<id>-main.service in <prefix>-<id>.slice (survives the client, the
              supervisor and any ssh session); supervisor = transient service <prefix>-<id>-sup.service running
              `bus supervise <id>`.
  supervise:  holds runs/<id>.lock (lockf) and makes one pass every PASS_S seconds: board walk and pin check; its own
              inbox and the bookings on its GPUs; class rules; one step of any release; the main service and the
              slice; private state; heartbeat. A pass that does not complete touches nothing.
  exec:       joins the slice as a scope only through admission (runs/<id>.admit.json under lockf).
  release:    close admission, wait for every exec intent, TERM the slice, KILL and stop it after the grace, then
              verify: slice gone or unpopulated, and no process of the slice on the entry's GPUs (nvidia-smi).
  fallback:   with no user manager, the client is the supervisor; no adoption and no hot update.
"""
import fcntl, json, os, queue, secrets, signal, subprocess, sys, threading, time

from . import bookings as bk
from . import core
from .core import Refused, now

STATE_VERSION = int(os.environ.get("AGENT_BUS_TEST_STATE_VERSION") or 1)
NONTERMINAL = ("starting", "running", "waiting-handoff", "stopping", "leader-exited", "needs-reconcile")
EXIT_RELEASED = 75


def _migrate_1(s):  # the test-only version 2 adds one field (design 8: migration from version 1 to a test version 2)
    s = dict(s, state_version=2)
    s.setdefault("migrated_from", 1)
    return s


MIGRATIONS = {1: _migrate_1}


def migrate(s):
    v = s.get("state_version")
    if not isinstance(v, int):
        raise Refused("private run state has no state_version")
    if v > STATE_VERSION:
        raise Refused(f"run state version {v} is newer than this build knows ({STATE_VERSION}); not supervising")
    while v < STATE_VERSION:
        s = MIGRATIONS[v](s)
        v = s["state_version"]
    return s


# ------------------------------------------------------------------------------------------------ private paths

def runs_dir():
    return core.private_dir("runs")


def state_path(rid):
    core.component(rid, "run id")
    return os.path.join(runs_dir(), f"{rid}.json")


def job_path(rid):
    core.component(rid, "run id")
    return os.path.join(runs_dir(), f"{rid}.job.json")


def ctl_dir(rid):
    core.component(rid, "run id")
    return core.private_dir("ctl", rid)


def load_state(rid):
    s = core.read_private(state_path(rid))
    if s is None:
        raise Refused(f"no run {rid} of yours (no private state)")
    return s


def build_root(path=None):
    """The build directory a `bus` entry point or this package lives in (builds/<id>/ or a source checkout)."""
    p = os.path.realpath(path or __file__)
    if path:
        return os.path.dirname(os.path.dirname(p))       # <build>/bin/bus
    return os.path.dirname(os.path.dirname(os.path.dirname(p)))  # <build>/lib/agentbus/runner.py


def build_id(root=None):
    return os.path.basename(root or build_root())


def own_bus():
    return os.path.join(build_root(), "bin", "bus")


def installed_build():
    b = core.BIN()
    if not os.path.exists(b):
        return None
    return build_id(build_root(b))


class Locked:
    """fcntl.lockf on a private file: exclusive; non-blocking or with a monotonic timeout."""

    def __init__(self, path, timeout=None):
        self.path, self.timeout, self.f = path, timeout, None

    def acquire(self):
        self.f = open(self.path, "a+")
        end = None if self.timeout is None else time.monotonic() + self.timeout
        while True:
            try:
                fcntl.lockf(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if end is None or time.monotonic() >= end:
                    self.f.close()
                    self.f = None
                    return False
                time.sleep(0.05)

    def release(self):
        if self.f:
            self.f.close()
            self.f = None

    def __enter__(self):
        if not self.acquire():
            raise Refused(f"could not take {os.path.basename(self.path)} within {self.timeout} s")
        return self

    def __exit__(self, *a):
        self.release()


def lock_free(path):
    l = Locked(path)
    if l.acquire():
        l.release()
        return True
    return False


# ------------------------------------------------------------------------------------------------ systemd and cgroups

def sysctl(*args, timeout=20):
    f = os.environ.get("AGENT_BUS_TEST_SYSTEMCTL_FAIL")
    if f and os.path.exists(f):            # tests: the user manager does not answer
        return 1, "", "test: systemctl failing"
    try:
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 255, "", repr(e)


def unit_props(unit, *props):
    return unit_query(unit, *props)[1]


def unit_query(unit, *props):
    """(ok, {prop: value}): ok False when systemctl itself failed, so an answer is never read from a failed query."""
    rc, out, _ = sysctl("show", unit, *[f"-p{p}" for p in props])
    d = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v
    return rc == 0 and bool(d), d


def user_manager_ok():
    if os.environ.get("AGENT_BUS_SYSTEMD") == "off":
        return False
    rc, out, _ = sysctl("is-system-running", timeout=10)
    return out.strip() in ("running", "degraded")


def systemd_run(args, env=None, timeout=30):
    try:
        r = subprocess.run(["systemd-run", "--user", "--quiet", *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout + r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 255, repr(e)


def passthrough_env():
    """AGENT_BUS_* settings a supervisor started as a transient unit must see as its starter did."""
    return [f"--setenv={k}={v}" for k, v in sorted(os.environ.items()) if k.startswith("AGENT_BUS_")]


CG_ROOT = "/sys/fs/cgroup"


def cgroup_of(unit, tries=1):
    for i in range(tries):
        cg = unit_props(unit, "ControlGroup").get("ControlGroup") or ""
        if cg.startswith("/"):
            return cg
        time.sleep(0.2 * (i + 1)) if i + 1 < tries else None
    return None


def main_unit_state(unit):
    """'exists' (systemd knows a started unit of that name), 'absent' (systemd positively reports none) or 'unknown'
    (the query failed)."""
    ok, p = unit_query(unit, "LoadState", "ActiveState", "ExecMainStartTimestampMonotonic")
    if not ok:
        return "unknown"
    if p.get("LoadState") == "not-found":
        return "absent"
    if p.get("LoadState") == "loaded" and (p.get("ActiveState") not in ("inactive", "") or
                                           int(p.get("ExecMainStartTimestampMonotonic") or 0) > 0):
        return "exists"
    return "absent"


def recover_cgroup(slice_unit):
    """The slice's cgroup path from systemd: its ControlGroup while active; when systemd positively reports it
    inactive or unknown, the path a slice of that name has (where nothing can be any more). None when the query
    fails: the caller then stays unverified rather than read the slice as gone."""
    ok, p = unit_query(slice_unit, "ControlGroup", "ActiveState", "LoadState")
    if not ok:
        return None
    if (p.get("ControlGroup") or "").startswith("/"):
        return p["ControlGroup"]
    if p.get("ActiveState") in ("inactive", "failed") or p.get("LoadState") == "not-found":
        uid = core.UID
        parent = slice_unit.split("-", 1)[0] + ".slice"
        return f"/user.slice/user-{uid}.slice/user@{uid}.service/{parent}/{slice_unit}"
    return None


def slice_state(cg_rel):
    """'gone' (ENOENT at the openat walk), 'empty' (cgroup.events populated 0), 'populated', or 'unknown' when no
    path is known (never read as gone)."""
    if not cg_rel:
        return "unknown"
    try:
        fd = core.walk(CG_ROOT + cg_rel)
    except FileNotFoundError:
        return "gone"
    except OSError:
        return "populated"
    try:
        r = core.read_file(fd, "cgroup.events", 4096)
    finally:
        os.close(fd)
    if r is None:
        return "populated"
    for line in r[0].decode().splitlines():
        k, _, v = line.partition(" ")
        if k == "populated":
            return "empty" if v.strip() == "0" else "populated"
    return "populated"


def cgroup_pids(cg_rel):
    """Every pid in the cgroup and below (for the report; the stop goes through systemd)."""
    out = []
    base = CG_ROOT + cg_rel
    for d, _, files in os.walk(base):
        if "cgroup.procs" in files:
            try:
                with open(os.path.join(d, "cgroup.procs")) as f:
                    out += [int(x) for x in f.read().split()]
            except OSError:
                pass
    return out


def nvidia_view(timeout=20):
    """(ok, {index: uuid}, [(uuid, pid)], {index: util}); ok False when either query fails or does not parse."""
    q = lambda *a: subprocess.run([core.NVSMI(), *a, "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                  timeout=timeout)
    try:
        g = q("--query-gpu=index,uuid,utilization.gpu")
        a = q("--query-compute-apps=gpu_uuid,pid")
    except (OSError, subprocess.TimeoutExpired):
        return False, {}, [], {}
    if g.returncode or a.returncode:
        return False, {}, [], {}
    idx, util, apps = {}, {}, []
    try:
        for line in g.stdout.splitlines():
            if not line.strip():
                continue
            i, uuid, u = [x.strip() for x in line.split(",")]
            idx[int(i)] = uuid
            util[int(i)] = int(u) if u.isdigit() else None
        for line in a.stdout.splitlines():
            if not line.strip() or "No running" in line:
                continue
            uuid, pid = [x.strip() for x in line.split(",")[:2]]
            apps.append((uuid, int(pid)))
    except ValueError:
        return False, {}, [], {}
    return True, idx, apps, util


def test_unreadable(pid):
    return str(pid) in (os.environ.get("AGENT_BUS_TEST_UNREADABLE_CGROUP") or "").split(",")


def gpu_check(gpus, in_job):
    """(clear, [job pids still on the GPUs], [other processes on them as 'pid:user'], error). `in_job(pid)` says
    whether a process is the job's (True), not the job's (False)."""
    ok, idx, apps, _ = nvidia_view()
    if not ok:
        return False, [], [], "nvidia-smi failed"
    uuids = {idx[g] for g in gpus if g in idx}
    if len(uuids) != len(set(gpus)):
        return False, [], [], "nvidia-smi does not list every GPU of the entry"
    mine, other = [], []
    for uuid, pid in apps:
        if uuid not in uuids:
            continue
        if in_job(pid):
            mine.append(pid)
        else:
            uid = core.proc_uid(pid)
            other.append(f"{pid}:{core.owner_name(uid) if uid is not None else '?'}")
    return not mine, sorted(set(mine)), sorted(set(other)), None


def other_runs_attribution(rid):
    """pid -> the id of another live run of this user that its own records show the pid belongs to (in its slice, a
    recorded escape of it, or its fallback process group), else None."""
    others = []
    for name in os.listdir(runs_dir()):
        if not name.endswith(".json") or name.endswith((".job.json", ".admit.json")) or name[:-5] == rid:
            continue
        st = core.read_private(os.path.join(runs_dir(), name)) or {}
        if st.get("state") in NONTERMINAL and st.get("host") == core.HOST:
            others.append(st)

    def attr(pid):
        cg = None if test_unreadable(pid) else core.proc_cgroup(pid)
        for st in others:
            if st.get("mode") == "systemd" and cg and in_tree(cg, (st.get("units") or {}).get("cgroup_path")):
                return st["id"]
            rec = (st.get("escaped") or {}).get(str(pid))
            if rec and core.identity_alive(rec):
                return st["id"]
            if st.get("mode") == "fallback" and st.get("pgid"):
                try:
                    if os.getpgid(pid) == st["pgid"]:
                        return st["id"]
                except OSError:
                    pass
        return None
    return attr


def gpu_release_check(rid, gpus, confirmed=()):
    """The GPU half of a release, conservative: (clear, unattributed pids of this user, other processes as 'pid:who',
    error). Clear only when nvidia-smi answers for every GPU and no process of this user is on them except those another
    live run of this user positively owns by its records, or that the owner confirmed are not this run's."""
    ok, idx, apps, _ = nvidia_view()
    if not ok:
        return False, [], [], "nvidia-smi failed"
    uuids = {idx[g] for g in gpus if g in idx}
    if len(uuids) != len(set(gpus)):
        return False, [], [], "nvidia-smi does not list every GPU of the entry"
    attr = other_runs_attribution(rid)
    mine, other = [], []
    by_uuid = {idx[g]: g for g in gpus if g in idx}
    gpu_check_result = {}
    for uuid, pid in apps:
        if uuid not in uuids:
            continue
        uid = core.UID if test_unreadable(pid) else core.proc_uid(pid)
        if uid is None:
            continue                       # gone between the query and the look
        if uid != core.UID:
            other.append(f"{pid}:{core.owner_name(uid)}")
        elif pid in confirmed:
            other.append(f"{pid}:{core.USER} (confirmed not this run)")
        elif attr(pid):
            other.append(f"{pid}:{core.USER} ({attr(pid)})")
        else:
            mine.append(pid)
            gpu_check_result.setdefault(by_uuid[uuid], set()).add(pid)
    gpu_release_check.held = {g: sorted(p) for g, p in gpu_check_result.items()}   # per GPU, for a partial hold
    return not mine, sorted(set(mine)), sorted(set(other)), None


def in_cgroup_fn(cg_rel, uid):
    """Whether a process is the job's by its cgroup: in the slice's cgroup or below, or ours with an unreadable cgroup
    (another user's process cannot be in this user's slice). Processes that left the slice are the supervisor's
    recorded escapes (Supervisor.track_escaped), not decided here."""
    def f(pid):
        cg = None if test_unreadable(pid) else core.proc_cgroup(pid)
        if cg is None:
            return core.proc_uid(pid) == uid or test_unreadable(pid)  # unreadable and ours: counts as the job's
        return in_tree(cg, cg_rel)
    return f


def in_tree(cg, cg_rel):
    return bool(cg_rel) and (cg == cg_rel or cg.startswith(cg_rel.rstrip("/") + "/"))


def _ppid(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def uid_procs(uid=None):
    """{pid: cgroup path} of every process of this uid whose cgroup can be read."""
    uid = core.UID if uid is None else uid
    out = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        try:
            if os.stat(f"/proc/{n}").st_uid != uid:
                continue
        except OSError:
            continue
        cg = core.proc_cgroup(n)
        if cg is not None:
            out[int(n)] = cg
    return out


def unit_of(cg):
    """The systemd unit a cgroup path belongs to (its last .scope or .service component), or None."""
    return next((c for c in reversed((cg or "").split("/")) if c.endswith((".scope", ".service"))), None)


def descendants_outside(cg_rel, known=(), procs=None):
    """Processes of this user outside the slice whose parent chain, as /proc shows it now, reaches a process inside
    the slice or one of `known` (pids already recorded as the job's). An Apptainer instance's init is one: systemd
    moves it into a scope of its own while the monitor that started it is still in the job."""
    procs = uid_procs() if procs is None else procs
    known = set(known)
    out = {}
    for pid, cg in procs.items():
        if in_tree(cg, cg_rel) or pid in known:
            continue
        p, n = pid, 0
        while n < 64:
            p = _ppid(p)
            if not p or p <= 1:
                break
            if (p in procs and in_tree(procs[p], cg_rel)) or p in known:
                out[pid] = cg
                break
            n += 1
    return out


def pidfd_signal(rec, sig):
    """Signal a recorded process through a pidfd, only while it is the recorded one. True when signalled."""
    if not core.identity_alive(rec):
        return False
    try:
        fd = os.pidfd_open(rec["pid"])
    except (OSError, AttributeError):
        return False
    try:
        if not core.identity_alive(rec):      # the pidfd pins the process now: check it is still the recorded one
            return False
        signal.pidfd_send_signal(fd, sig)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------ admission

def admit_paths(rid):
    core.component(rid, "run id")
    d = runs_dir()
    return os.path.join(d, f"{rid}.admit.json"), os.path.join(d, f"{rid}.admit.lock")


def admit_read(rid):
    return core.read_private(admit_paths(rid)[0]) or {"run": None, "admission": "closed", "inflight": []}


def admit_write(rid, a):
    core.write_private(admit_paths(rid)[0], a)


def admit_lock(rid, timeout=10):
    return Locked(admit_paths(rid)[1], timeout)


def scope_unit(rid, eid):
    return f"{core.unit_name(rid)}-exec-{eid}.scope"


def unit_exists(unit):
    p = unit_props(unit, "LoadState", "ActiveState")
    return p.get("LoadState") == "loaded" and p.get("ActiveState") not in (None, "", "inactive", "failed")


def cmd_exec(rid, run, argv, cwd=None):
    """bus exec: admitted, launching, then registered once the scope is in the slice (or cancelled)."""
    s = load_state(rid)
    if s.get("mode") != "systemd":
        raise Refused(f"{rid} runs in fallback mode (no user manager): no exec into it")
    eid = secrets.token_hex(6)
    with admit_lock(rid):
        a = admit_read(rid)
        if a.get("admission") != "open":
            raise Refused(f"admission to {rid} is closed (the run is stopping or has ended)")
        if a.get("run") != run:
            raise Refused(f"{rid} is now run {a.get('run')}, not {run}: refusing a stale exec")
        ident = core.proc_identity(os.getpid())
        a["inflight"].append(dict(exec_id=eid, client=ident, state="admitted", t=now()))
        admit_write(rid, a)
    if os.environ.get("AGENT_BUS_TEST_EXEC_PAUSE") == "admitted":
        os.kill(os.getpid(), signal.SIGSTOP)
    with admit_lock(rid):
        a = admit_read(rid)
        it = next((x for x in a["inflight"] if x["exec_id"] == eid), None)
        if not it or it["state"] != "admitted":
            raise Refused(f"exec {eid} was cancelled before it started")
        it["state"] = "launching"
        admit_write(rid, a)
    if os.environ.get("AGENT_BUS_TEST_EXEC_PAUSE") == "launching":
        os.kill(os.getpid(), signal.SIGSTOP)
    unit = scope_unit(rid, eid)
    p = subprocess.Popen(["systemd-run", "--user", "--quiet", "--scope", f"--slice={s['units']['slice']}",
                          f"--unit={unit}", "--", *argv],
                         cwd=cwd or None)
    registered = False
    while True:
        if not registered and unit_exists(unit):
            registered = True
            with admit_lock(rid, timeout=30):
                a = admit_read(rid)
                for x in a["inflight"]:
                    if x["exec_id"] == eid:
                        x["state"] = "registered"
                admit_write(rid, a)
        if p.poll() is not None:
            break
        time.sleep(0.1)
    if not registered:
        with admit_lock(rid, timeout=30):
            a = admit_read(rid)
            for x in a["inflight"]:
                if x["exec_id"] == eid and x["state"] != "registered":
                    x["state"] = "cancelled"
            admit_write(rid, a)
    return p.returncode


def resolve_intents(rid):
    """Under the admit lock: cancel intents whose client is gone and whose scope does not exist. Returns the exec ids
    still outstanding (admitted or launching with a live client, or launching whose scope is being created)."""
    with admit_lock(rid, timeout=10):
        a = admit_read(rid)
        a["admission"] = "closed"
        out = []
        for x in a.get("inflight", []):
            if x["state"] in ("registered", "cancelled"):
                continue
            gone = not core.identity_alive(x.get("client"))
            if x["state"] == "launching" and unit_exists(scope_unit(rid, x["exec_id"])):
                x["state"] = "registered"
            elif gone:
                x["state"] = "cancelled"
            else:
                out.append(x["exec_id"])
        admit_write(rid, a)
        return out


# ------------------------------------------------------------------------------------------------ hooks and notices

class Hooks:
    """Runs the --on-message executable with one JSON object on stdin per event, killed after HOOK_TIMEOUT_S, in a
    thread of its own so a hook that never returns never holds up a pass."""

    def __init__(self, exe):
        self.exe = exe
        self.q = queue.Queue()
        if exe:
            threading.Thread(target=self._loop, daemon=True).start()

    def __call__(self, event):
        if self.exe:
            self.q.put(event)

    def _loop(self):
        while True:
            ev = self.q.get()
            try:
                p = subprocess.Popen([self.exe], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                continue
            try:
                p.communicate(json.dumps(ev).encode(), timeout=core.HOOK_TIMEOUT_S())
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
            except OSError:
                pass


def notice(rid, s, hooks, event, **kw):
    """Tell the owner: the hook, a line in notices.jsonl, and the run state's last notices (what `bus watch` prints)."""
    ev = dict(event=event, id=rid, run=s.get("run"), time=now(), **kw)
    hooks(ev)
    try:
        with open(os.path.join(core.private_dir(), "notices.jsonl"), "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass
    s.setdefault("notices", []).append(ev)
    s["notices"] = s["notices"][-50:]


# ------------------------------------------------------------------------------------------------ the supervisor

class Supervisor:
    def __init__(self, rid, adopt=False, fallback_child=None):
        self.rid = core.component(rid, "run id")      # before any path is built from it
        self.adopt = adopt
        self.child = fallback_child      # fallback: (pid, pgid, pidfd) of the job the client forked
        self.lock = Locked(os.path.join(runs_dir(), f"{rid}.lock"))
        self.passes = 0
        self.enoent = 0
        self.hooks = None
        self.s = None

    # --- state

    def save(self):
        core.write_private(state_path(self.rid), self.s)

    def entry(self):
        s = self.s
        e = dict(id=s["id"], user=core.USER, host=s["host"], gpus=s.get("held_gpus") or s["gpus"], **{"class": s["class"]},
                 booking=s.get("booking"), run=s["run"], started=s["started"], until=s["until"], kind=s["kind"],
                 exclusive=bool(s.get("exclusive")), note=s.get("note") or "", state=s["entry_state"],
                 schema=core.SCHEMA)
        if s.get("stop_cost_min") is not None:
            e["stop_cost_min"] = s["stop_cost_min"]
        if s.get("booking_revision"):
            e["booking_revision"] = s["booking_revision"]
        if s.get("idle_backfill"):
            e["idle"] = True
        return e

    def write_entry(self, board):
        board.writable()
        data = core.dumps(self.entry())
        if data != self.s.get("_entry_written"):
            core.replace(board.wdir("agents"), f"{self.rid}.json", data)
            self.s["_entry_written"] = data.decode()
            self.s["entry_on_board"] = True

    def archive_entry(self, board):
        if not board.ok or not self.s.get("entry_on_board"):
            return
        afd, gfd = board.dir("agents"), board.dir("archive")
        dest = f"{self.rid}-{now()}.json"
        try:
            os.rename(f"{self.rid}.json", dest, src_dir_fd=afd, dst_dir_fd=gfd)
            e = dict(self.entry(), state="ended", end_reason=self.s.get("end_reason"), ended=now())
            core.replace(gfd, dest, core.dumps(e))
        except OSError:
            pass
        hb = board.dir("agents", ".heartbeat")
        if hb is not None:
            core.unlink_own(hb, self.rid)
        self.s["entry_on_board"] = False
        board.log("ended", entry=self.rid, reason=self.s.get("end_reason"))

    def reply(self, board, req, answer, text):
        """A reply message into the requester's own mailbox (its reply_to, checked to be theirs)."""
        if not board.ok or board.inbox_owner(req["reply_to"]) != req["sender"]:
            return
        mid = core.new_id(f"re-{secrets.token_hex(3)}")
        m = dict(id=mid, sender=core.USER, to=req["reply_to"], kind="reply", request_id=req["request_id"],
                 reply_to=None, answer=answer, reason=text if answer == "no" else None, note=text[:core.MSG_NOTE_MAX],
                 gpus=self.s["gpus"], minutes=None, from_entry=self.rid, schema=core.SCHEMA, created=now())
        try:
            core.publish(board.dir("inbox", req["reply_to"]), f"{mid}.json", core.dumps(m))
        except (OSError, TypeError):
            pass

    # --- class rules

    def protected(self, rows, t):
        """Inside its own standing, active, unlapsed booking window."""
        s = self.s
        if s["class"] != "scheduled" or not s.get("booking"):
            return False, None
        b = next((r for r in rows if r["id"] == s["booking"]), None)
        if b and b["user"] == core.USER and b.get("state") == "standing" and set(s["gpus"]) <= set(b["gpus"]) \
                and max(b["start"], b["objection_close"]) <= t < b["end"]:
            return True, b
        return False, b

    def authorized(self, board, m, prot):
        """Design 3.10: why a request may not stop the run, or None when it may."""
        if m.get("_mode") != 0o644:
            return "advisory: the message file is not 0644"
        if board.inbox_owner(m["reply_to"]) != m["sender"]:
            return "advisory: its reply_to is not the sender's own mailbox"
        if m.get("to_run") != self.s["run"]:
            return "stale: it names another run of this id"
        if not board.ok:
            return "board identity changed"
        if prot:
            return "protected"
        return None

    # --- release

    def start_release(self, reason, requesters=()):
        s = self.s
        if s.get("release"):
            s["release"]["requesters"] = sorted(set(s["release"]["requesters"]) | set(requesters))
            return
        s["release"] = dict(step="close", reason=reason, started_wall=time.time(), requesters=list(requesters),
                            term_wall=None, kill_wall=None, remains=None, told=0,
                            job_exited_before=s.get("exit_status") is not None)
        s["entry_state"] = "stopping"
        s["state"] = "stopping"
        notice(self.rid, s, self.hooks, "release-started", reason=reason)

    def track_escaped(self):
        """Record, with their identity, the job's processes that left the slice (design 3.3 assumed none could): a
        process outside the slice whose parent chain reaches the slice or an already recorded escape. Only these
        records make a process outside the slice the job's; records whose process is gone are dropped."""
        s = self.s
        cg = s["units"].get("cgroup_path")
        if s["mode"] != "systemd" or not cg:
            return {}
        rec = {k: v for k, v in (s.get("escaped") or {}).items() if core.identity_alive(v)}
        procs = uid_procs()
        for pid, pcg in descendants_outside(cg, [int(k) for k in rec], procs).items():
            ident = core.proc_identity(pid)
            if ident:
                rec[str(pid)] = dict(ident, unit=unit_of(pcg), cgroup=pcg, seen=now())
        s["escaped"] = rec
        return rec

    def escaped_live(self):
        return {int(k): v for k, v in (self.s.get("escaped") or {}).items() if core.identity_alive(v)}

    def stop_signal(self, sig):
        s = self.s
        if s["mode"] == "systemd":
            sysctl("kill", f"--signal={sig}", s["units"]["slice"])
            esc = self.escaped_live()
            killed_units = set()
            for pid, rec in esc.items():
                unit = rec.get("unit")
                if unit and unit not in killed_units and self.unit_exclusive(rec.get("cgroup"), unit, esc):
                    sysctl("kill", f"--signal={sig}", unit)   # every process in that unit is a recorded job process
                    killed_units.add(unit)
            for pid, rec in esc.items():
                pidfd_signal(rec, getattr(signal, f"SIG{sig}"))
        else:
            try:
                os.killpg(self.child[1], getattr(signal, f"SIG{sig}"))
            except (OSError, TypeError):
                pass

    @staticmethod
    def unit_exclusive(cg, unit, esc):
        """True only when every process of the unit (its own cgroup and everything below it, not just the subtree the
        recorded process sits in) is a recorded process of this run."""
        if not cg or not unit or not cg.startswith("/"):
            return False
        parts = cg.split("/")
        if unit not in parts:
            return False
        top = "/".join(parts[:parts.index(unit) + 1])
        pids = cgroup_pids(top)
        return bool(pids) and all(p in esc for p in pids)

    def group_state(self):
        """Fallback: 'gone' when the job's process group is empty and the leader has been reaped."""
        self.reap()
        try:
            os.killpg(self.child[1], 0)
            return "populated"
        except ProcessLookupError:
            return "gone"
        except (OSError, TypeError):
            return "gone"

    def reap(self):
        if self.child and self.s.get("exit_status") is None:
            try:
                pid, status = os.waitpid(self.child[0], os.WNOHANG)
            except ChildProcessError:
                pid, status = self.child[0], 0
            if pid:
                self.s["exit_status"] = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") \
                    else (status >> 8)
                if self.s["exit_status"] < 0:
                    self.s["exit_status"] = 128 - self.s["exit_status"]

    def verify(self):
        """(released, remains). Slice gone (ENOENT twice, one pass apart) or unpopulated, and GPUs clear."""
        s = self.s
        remains = []
        if s["mode"] == "systemd":
            st = slice_state(s["units"]["cgroup_path"])
            if st == "unknown":
                remains.append("slice cgroup path unknown (systemd query failed); not verified")
                gone = False
            elif st == "gone":
                self.enoent += 1
                gone = self.enoent >= 2
                if not gone:
                    remains.append("slice gone once; confirming next pass")
            if st not in ("gone", "unknown"):
                self.enoent = 0
                gone = st == "empty"
                if not gone:
                    pids = cgroup_pids(s["units"]["cgroup_path"])
                    remains.append(f"slice populated (pids {','.join(map(str, pids[:8])) or '?'})")
            self.track_escaped()
            esc = self.escaped_live()
            if esc:   # a recorded process of the job still lives: not released (ambiguity keeps the run stopping)
                gone = False
                remains.append("job processes outside the slice: " + ", ".join(f"{p} ({r.get('unit')})" for p, r in
                                                                                sorted(esc.items())[:8]))
        else:
            gone = self.group_state() == "gone"
            if not gone:
                remains.append("process group still has processes")
        gpus = s["gpus"]            # every one of the run's original GPUs, a partially released one included
        confirmed = set(s.get("reconciled", []))
        clear, mine, other, err = gpu_release_check(self.rid, gpus, confirmed)
        held = dict(getattr(gpu_release_check, "held", {}) or {}) if mine else {}
        unres = s.setdefault("unresolved", {})
        for pid in mine:                     # remembered by identity until it exits or the owner confirms it
            ident = core.proc_identity(pid)
            if ident and str(pid) not in unres:
                unres[str(pid)] = ident
        for k in list(unres):
            if int(k) in confirmed or not core.identity_alive(unres[k]):
                del unres[k]
        alive_unres = sorted(int(k) for k in unres)
        if alive_unres and not err:
            clear = False                   # alive anywhere, on a GPU now or not: the run is not released
            if not mine:
                remains.append(f"unresolved processes of {core.USER} still alive: {','.join(map(str, alive_unres))}"
                               f" (bus reconcile {self.rid})")
                held = {g: [] for g in (s.get("held_gpus") or [])}
            mine = sorted(set(mine) | set(alive_unres))
        if err:
            remains.append(err)
        elif mine:
            remains.append(f"unattributed processes of {core.USER} on GPUs {core.gpu_text(s['gpus'])}: "
                           f"{','.join(map(str, mine))} (bus reconcile {self.rid})")
        s["_other_on_gpus"] = other
        # everything of the job is gone and only processes nobody's records explain remain: the owner decides
        s["_reconcile"] = dict(pids=mine, held=sorted(held) or sorted(s.get("held_gpus") or gpus),
                               released=sorted(set(gpus) - set(held or s.get("held_gpus") or gpus))) \
            if (gone and not err and mine) else None
        return gone and clear, remains

    def mark_reconcile(self, board=None):
        """Hold only the GPUs that carry a process nobody's records explain; the run's other GPUs are released now
        (the entry is narrowed to the held ones, and whoever asked is told which are free of this job)."""
        s = self.s
        rc = s.get("_reconcile")
        if not rc:
            return
        newly = [g for g in rc["released"] if g not in (s.get("gpus_released") or [])]
        reheld = [g for g in rc["held"] if g in (s.get("gpus_released") or [])]
        if s["state"] != "needs-reconcile" or newly or reheld or rc["held"] != s.get("held_gpus"):
            s["state"] = s["entry_state"] = "needs-reconcile"
            s["held_gpus"] = rc["held"]
            s["gpus_released"] = rc["released"]     # a GPU an unresolved process moved onto is held again
            notice(self.rid, s, self.hooks, "needs-reconcile", pids=rc["pids"], gpus_held=rc["held"],
                   gpus_released=s["gpus_released"],
                   action=f"stop them, or confirm they are not this run's: bus reconcile {self.rid} --confirm <pid>")
            if board is not None and newly:
                for req in (s.get("release") or {}).get("requesters_full", []):
                    self.reply(board, req, "yes", f"GPUs {core.gpu_text(newly)} free of this job; GPUs "
                                                  f"{core.gpu_text(rc['held'])} held: a process of ours there is "
                                                  f"not attributed to any run")
        s["needs_reconcile"] = rc["pids"]

    def release_step(self, board):
        s, r = self.s, self.s["release"]
        t = time.time()
        if r["step"] == "close":
            self.track_escaped()     # before the stop: the processes that link an escape to the slice die with it
            out = resolve_intents(self.rid) if s["mode"] == "systemd" else []
            if out:
                r["remains"] = [f"exec {e} outstanding" for e in out]
                return self.tell_stopping(board)
            self.stop_signal("TERM")
            r.update(step="term", term_wall=t)
            return
        if r["step"] == "term":
            if s["mode"] == "systemd":
                self.track_escaped()
            gone = (slice_state(s["units"]["cgroup_path"]) in ("gone", "empty") and not self.escaped_live()) \
                if s["mode"] == "systemd" else self.group_state() == "gone"
            if not gone and t - r["term_wall"] < core.TERM_GRACE_S():
                return
            if not gone:
                self.stop_signal("KILL")
                if s["mode"] == "systemd":
                    sysctl("stop", s["units"]["slice"])
            r.update(step="verify", kill_wall=t)
        if r["step"] == "verify":
            if s["mode"] == "systemd" and t - (r["kill_wall"] or t) > 1 and (
                    slice_state(s["units"]["cgroup_path"]) == "populated" or self.escaped_live()):
                self.stop_signal("KILL")        # anything placed in the slice after the stop is the run's too
            ok, remains = self.verify()
            r["remains"] = remains
            if ok:
                return self.finish(board, released=True)
            self.mark_reconcile(board)
            self.tell_stopping(board)

    def tell_stopping(self, board):
        r = self.s["release"]
        if time.time() - (r.get("told") or 0) >= 60:
            r["told"] = time.time()
            what = "; ".join(r.get("remains") or []) or "checking"
            notice(self.rid, self.s, self.hooks, "stopping", remains=r.get("remains"))
            for req in r.get("requesters_full", []):
                self.reply(board, req, "yes", f"stopping, not yet free: {what}")

    def finish(self, board, released):
        """The run is free of its GPUs: state ended, entry archived, units cleaned up, the requesters told."""
        s = self.s
        if released:
            s["end_reason"] = s["release"]["reason"]
            if not s["release"].get("job_exited_before"):
                s["ended_by_release"] = True          # exit_status null: the job did not exit on its own
                s["job_status_after_release"] = s.get("exit_status")
                s["exit_status"] = None
        other = s.get("_other_on_gpus") or []
        text = ((f"released: GPUs {core.gpu_text(s['gpus'])} free of this job" if released
                 else f"ended ({s.get('end_reason')}); GPUs {core.gpu_text(s['gpus'])} free of this job")
                + (f" (other processes on them: {', '.join(other)})" if other else ""))
        for req in (s.get("release") or {}).get("requesters_full", []):
            self.reply(board, req, "yes", text)
        s["state"] = "ended"
        s["entry_state"] = "ended"
        s["ended"] = now()
        self.archive_entry(board)
        if s["mode"] == "systemd":
            sysctl("stop", s["units"]["main"])
            sysctl("reset-failed", s["units"]["main"])
            sysctl("stop", s["units"]["slice"])
        notice(self.rid, s, self.hooks, "released" if released else "ended", reason=s.get("end_reason"),
               exit_status=s.get("exit_status"), text=text)
        if released:
            notice(self.rid, s, self.hooks, "ended", reason=s.get("end_reason"), exit_status=s.get("exit_status"))
        self.save()

    # --- the main service

    def main_status(self):
        """None while the job's leader runs; else its exit status (128+signal for a signal)."""
        s = self.s
        if s["mode"] == "fallback":
            self.reap()
            return s.get("exit_status")
        p = unit_props(s["units"]["main"], "LoadState", "ActiveState", "SubState", "ExecMainStatus", "ExecMainCode",
                       "ExecMainExitTimestampMonotonic", "MainPID")
        if p.get("LoadState") == "not-found":
            return s.get("exit_status") if s.get("exit_status") is not None else -1
        if int(p.get("ExecMainExitTimestampMonotonic") or 0) > 0 and p.get("MainPID") in ("0", None):
            code, st = int(p.get("ExecMainCode") or 0), int(p.get("ExecMainStatus") or 0)
            return 128 + st if code in (2, 3) else st
        return None

    def start_main(self):
        s = self.s
        rc, out = start_main_unit(self.rid, s)
        if rc:
            raise Refused(f"systemd-run for the job failed: {out.strip()[:300]}")
        self.handoff_done()

    def handoff_done(self):
        s = self.s
        s["units"]["cgroup_path"] = s["units"].get("cgroup_path") or cgroup_of(s["units"]["slice"], tries=5) \
            or recover_cgroup(s["units"]["slice"])
        s.pop("handoff_starting", None)
        s.pop("handoff_waits", None)
        s["state"] = s["entry_state"] = "running"
        s["main_started"] = s.get("main_started") or now()
        with admit_lock(self.rid):       # exec opens only once the handoff is done and the job runs
            a = admit_read(self.rid)
            if a.get("admission") == "handoff":
                a["admission"] = "open"
                admit_write(self.rid, a)

    # --- one pass

    def one_pass(self):
        s = self.s
        t0 = time.monotonic()
        t = now()
        try:
            board = core.Board(s["board"]["path"], (s["board"]["inode"], s["board"]["fsid"]))
        except OSError as e:
            board = None
            err = core.errno_name(e)
        with_board = board is not None and board.ok
        if not with_board:
            if not s.get("identity_alarm"):
                s["identity_alarm"] = t
                notice(self.rid, s, self.hooks, "board-identity-changed",
                       detail=f"{s['board']['path']}: " + (err if board is None else "inode or filesystem id differs"),
                       action="nothing is written to the board and no board record acts on this run; the job keeps "
                              "running")
        elif s.get("identity_alarm"):
            s.pop("identity_alarm")
            notice(self.rid, s, self.hooks, "board-identity-restored")
        try:
            if s["state"] == "starting" and self.resolve_starting() != "running":
                if s["state"] == "ended":
                    return
                s["last_pass"] = t
                self.save()
                return
            if s["mode"] == "systemd" and s["state"] in ("running", "leader-exited", "stopping", "needs-reconcile") \
                    and not s["units"].get("cgroup_path"):
                s["units"]["cgroup_path"] = recover_cgroup(s["units"]["slice"])
                if not s["units"]["cgroup_path"]:
                    s["pending_verification"] = "the slice's cgroup path is unknown and systemd did not answer"
                else:
                    s.pop("pending_verification", None)
            # owner's release (private, not a board record)
            for name in sorted(os.listdir(ctl_dir(self.rid))):
                if name.startswith("release-"):
                    d = core.read_private(os.path.join(ctl_dir(self.rid), name)) or {}
                    if s["state"] in NONTERMINAL:
                        self.start_release(d.get("reason") or "released by its owner")
                    os.unlink(os.path.join(ctl_dir(self.rid), name))
                elif name.startswith("reconcile-"):
                    d = core.read_private(os.path.join(ctl_dir(self.rid), name)) or {}
                    s["reconciled"] = sorted(set(s.get("reconciled", [])) | set(d.get("pids") or []))
                    os.unlink(os.path.join(ctl_dir(self.rid), name))
            rows, entries = [], []
            if with_board:
                entries = bk.entries_with_occupancy(board)
                msgs = board.messages(self.rid)
                rows = self.board_rows(board, t, entries, msgs)
                for r in rows:
                    key = f"{r['id']}@{r.get('revision')}"
                    if r.get("state") == "lapsed" and key not in s.setdefault("lapsed", []):
                        s["lapsed"].append(key)
                if time.monotonic() - t0 > core.PASS_BUDGET_S():
                    raise TimeoutError("board work over its deadline")
            prot, own = self.protected(rows, t)
            if own and s.get("booking_revision") in [own.get("revision")] + (own.get("lineage") or []):
                s["booking_revision"] = own["revision"]   # a narrowing revision keeps the run's tie to its booking
            if own and s.get("arrival_recorded") and s.get("arrival_revision") != own["revision"]:
                s["arrival_recorded"] = False       # a new revision of the booking needs its own arrival
            if (with_board and own and s["class"] == "scheduled" and own["user"] == core.USER
                    and own.get("state") == "standing" and not s.get("arrival_recorded")
                    and set(s["gpus"]) <= set(own["gpus"]) and s.get("booking_revision") == own["revision"]
                    and s["state"] in ("running", "waiting-handoff")
                    and max(own["start"], own["objection_close"]) <= t <= own.get("lapse_at", t)):
                s["arrival_recorded"] = bk.record_arrival(board, own["id"], own["revision"], self.rid, s["run"], t)
                s["arrival_revision"] = own["revision"]
            s["protected_until"] = own["end"] if prot else None
            if own and s["class"] == "scheduled" and own.get("state") == "objected" and not s.get("told_objected"):
                s["told_objected"] = True
                o = (own.get("objections") or [{}])[0]
                notice(self.rid, s, self.hooks, "booking-objected", booking=own["id"], why=own.get("why"),
                       reason=o.get("reason"), objector=o.get("user"))
            # handoff: an exclusive scheduled run waits for the booked GPUs to be free of everyone
            if s["state"] == "waiting-handoff":
                known = main_unit_state(s["units"]["main"])      # the unit's real state comes first
                # the booking is over only on evidence: a readable board showing it gone or past its end. An
                # unreadable or replaced board defers every decision; absence of evidence stops nothing
                booking_over = with_board and (own is None or t >= own.get("end", 0))
                if known == "exists":
                    # started by a supervisor that died before saving it: the job runs, so it is finished as a
                    # handoff and, if its booking is gone, stopped through the verified release, never just ended
                    self.handoff_done()
                    notice(self.rid, s, self.hooks, "started", note="handoff complete (recovered after adoption)")
                    if booking_over:
                        self.start_release("the booking ended while the GPUs were being handed over")
                elif known == "absent" and booking_over:
                    s["end_reason"] = "the booking ended before the GPUs were handed over"
                    return self.finish(board if with_board else _NoBoard(), released=False)
                elif known == "absent" and not with_board:
                    s["handoff_waits"] = ["board unreadable or its identity changed: waiting"]
                elif known == "absent":
                    # a first start, or one a crash interrupted: either way the GPUs are checked again first
                    waits = handoff_waits(board, entries, s) if with_board else ["board identity changed"]
                    s["handoff_waits"] = waits
                    if not waits:
                        s["handoff_starting"] = True
                        self.save()          # progress first: a crash from here on finds the unit, not a new start
                        self.start_main()
                        notice(self.rid, s, self.hooks, "started", note="handoff complete")
                else:
                    s["pending_verification"] = "systemd did not answer about the job's unit"
            # requests and bookings (board records): only with the board's identity intact
            if with_board and s["state"] in ("running", "leader-exited", "waiting-handoff", "needs-reconcile") \
                    and not s.get("release"):
                for m in msgs:
                    if m["kind"] != "request" or m["request_id"] in s["requests"]:
                        continue
                    why = self.authorized(board, m, prot)
                    s["requests"][m["request_id"]] = dict(first_seen=t, sender=m["sender"], msg=m["id"],
                                                          state="release" if why is None else why)
                    if why is None:
                        req = dict(sender=m["sender"], reply_to=m["reply_to"], request_id=m["request_id"])
                        self.start_release(f"request {m['id']} from {m['sender']}", [m["sender"]])
                        s["release"].setdefault("requesters_full", []).append(req)
                        notice(self.rid, s, self.hooks, "request", sender=m["sender"], msg=m["id"],
                               gpus=m.get("gpus"), minutes=m.get("minutes"), note=m.get("note", ""))
                        break
                    if why == "protected":
                        self.reply(board, m, "no", f"scheduled run inside its standing booking {own['id']} until "
                                                   f"{time.strftime('%m-%d %H:%M', time.localtime(own['end']))}")
                        notice(self.rid, s, self.hooks, "request", sender=m["sender"], msg=m["id"], refused=why)
                if not s.get("release") and not prot:
                    yield_to = bk.standing_active_on(rows, s["gpus"], t, exclude=s.get("booking"))
                    if yield_to:
                        b = yield_to[0]
                        self.start_release(f"yielded to booking {b['id']}")
                        board.log("yield", entry=self.rid, booking=b["id"], booker=b["user"])
            if s.get("release"):
                self.release_step(board if with_board else _NoBoard())
                if s["state"] == "ended":
                    return
            # the main service and the slice
            if s["state"] in ("running", "leader-exited", "needs-reconcile") and not s.get("release"):
                st = self.main_status()
                if st is not None:
                    s["exit_status"] = st if st >= 0 else s.get("exit_status")
                    out = resolve_intents(self.rid) if s["mode"] == "systemd" else []
                    ok, remains = (False, [f"exec {e} outstanding" for e in out]) if out else self.verify()
                    if ok:
                        s["end_reason"] = f"exited rc={s.get('exit_status')}"
                        return self.finish(board if with_board else _NoBoard(), released=False)
                    if s["state"] not in ("leader-exited", "needs-reconcile"):
                        s["state"] = s["entry_state"] = "leader-exited"
                        notice(self.rid, s, self.hooks, "leader-exited", exit_status=st, remains=remains)
                    if not out:
                        self.mark_reconcile(board if with_board else None)
                    s["leader_remains"] = remains
            if s["mode"] == "systemd" and s["state"] in ("running", "leader-exited"):
                self.track_escaped()
            self.passes += 1
            if self.passes % core.SAMPLE_EVERY() == 1 % core.SAMPLE_EVERY() and s["state"] == "running":
                self.sample()
            if with_board and s["state"] in NONTERMINAL:
                self.write_entry(board)
                hb = board.wdir("agents", ".heartbeat")
                core.touch(hb, self.rid)
            s["last_pass"] = t
            s["last_pass_s"] = round(time.monotonic() - t0, 3)
            s["max_pass_s"] = max(s.get("max_pass_s") or 0, s["last_pass_s"])
            self.save()
        finally:
            if board is not None:
                board.close()

    def board_rows(self, board, t, entries, msgs):
        """Booking states for this pass, from every receipt read and hashed now. Each pass decides whether the run
        continues (a backfill run yields to a standing booking it must see), so no pass trusts a cache."""
        return bk.evaluate(board, t, entries=entries, sticky_lapsed=set(self.s.get("lapsed", [])), cache=None)

    def sample(self):
        """Every SAMPLE_EVERY passes: the GPUs' utilization, kept privately; under 5% for 30 min marks idle backfill."""
        s = self.s
        ok, idx, apps, util = nvidia_view()
        if not ok:
            return
        u = max((util.get(g) or 0) for g in s["gpus"])
        try:
            with open(os.path.join(core.private_dir("usage"), f"{self.rid}.jsonl"), "a") as f:
                f.write(json.dumps(dict(t=now(), util={g: util.get(g) for g in s["gpus"]})) + "\n")
        except OSError:
            pass
        if u >= 5:
            s.pop("idle_since", None)
            s["idle_backfill"] = False
        else:
            s.setdefault("idle_since", now())
            s["idle_backfill"] = s["class"] == "backfill" and now() - s["idle_since"] >= core.IDLE_S()

    # --- the loop

    def run(self):
        if not self.lock.acquire():
            print(f"bus: another supervisor holds {self.rid}; this one exits", file=sys.stderr)
            return 3
        try:
            raw = load_state(self.rid)
            try:
                self.s = migrate(raw)
            except Refused as e:
                with open(os.path.join(core.private_dir(), "notices.jsonl"), "a") as f:
                    f.write(json.dumps(dict(event="refused", id=self.rid, time=now(), detail=str(e))) + "\n")
                print(f"bus: {e}", file=sys.stderr)
                return 1
            s = self.s
            if s.get("host") and s["host"] != core.HOST:
                print(f"bus: {self.rid} runs on {s['host']}, not here", file=sys.stderr)
                return 1
            self.hooks = Hooks(s.get("on_message"))
            prev = s.get("supervisor") or {}
            s["supervisor"] = dict(code=build_id(), pid=os.getpid(), boot_id=core.boot_id(),
                                   ticks=core.proc_ticks(os.getpid()))
            if self.adopt:
                s.setdefault("adoptions", []).append(dict(t=time.time(), code=build_id(), previous=prev.get("code")))
                s["adoptions"] = s["adoptions"][-20:]
            if self.resolve_starting() == "ended":
                return 0
            self.save()
            while s["state"] in NONTERMINAL:
                start = time.monotonic()
                try:
                    if os.environ.get("AGENT_BUS_TEST_STALL") and os.path.exists(os.environ["AGENT_BUS_TEST_STALL"]):
                        time.sleep(core.PASS_S())
                        continue
                    self.one_pass()
                except (OSError, TimeoutError, ValueError, KeyError, Refused) as e:
                    # an incomplete pass touches nothing; the next pass tries again
                    s.setdefault("pass_errors", []).append(f"{now()} {type(e).__name__}: {str(e)[:200]}")
                    s["pass_errors"] = s["pass_errors"][-20:]
                if s["state"] not in NONTERMINAL:
                    break
                self.maybe_upgrade()
                time.sleep(max(0.0, core.PASS_S() - (time.monotonic() - start)))
            return 0
        finally:
            self.lock.release()

    def resolve_starting(self):
        """A run found in `starting` (its client died early): running when systemd shows its main unit, ended only when
        systemd positively reports there is none; a failed query leaves it starting, to be asked again next pass."""
        s = self.s
        if s["state"] != "starting" or s["mode"] != "systemd":
            return s["state"]
        if s.get("handoff"):
            s["state"] = s["entry_state"] = "waiting-handoff"
            return s["state"]
        known = "exists" if s.get("main_started") else main_unit_state(s["units"]["main"])
        if known == "exists":
            s["state"] = s["entry_state"] = "running"
        elif known == "absent":
            s["end_reason"] = "its units never started"
            s["state"] = s["entry_state"] = "ended"
            self.save()
        else:
            s["pending_verification"] = "systemd did not answer about the job's unit; still starting"
        return s["state"]

    def maybe_upgrade(self):
        """Hot update: when the installed build differs from this one, save, drop the lock and exec it."""
        if self.s["mode"] != "systemd":
            return
        inst = installed_build()
        if not inst or inst == build_id():
            return
        target = os.path.realpath(core.BIN())
        self.save()
        self.lock.release()
        self.s["exec_at"] = time.time()
        core.write_private(state_path(self.rid), self.s)
        try:
            sys.stdout.flush()
            os.execv(sys.executable, [sys.executable, "-IB", target, "supervise", self.rid, "--adopt"])
        except OSError as e:
            with open(os.path.join(core.private_dir(), "notices.jsonl"), "a") as f:
                f.write(json.dumps(dict(event="upgrade-exec-failed", id=self.rid, time=now(), detail=repr(e))) + "\n")
            os._exit(1)   # the adopt timer takes over


class _NoBoard:
    """Stands in for the board while its identity is changed: every write is suppressed."""
    ok = False

    def log(self, *a, **k):
        pass

    def inbox_owner(self, box):
        return None

    def dir(self, *a):
        return None


def handoff_waits(board, entries, s):
    """What an exclusive scheduled launch still waits for: every other entry on its GPUs (an entry is on the board until
    its owner's release has been verified and it is archived, so a stale heartbeat does not end the wait), every GPU
    that nvidia-smi does not identify, and any process of anyone on them."""
    waits = []
    for e in entries:
        if e["id"] != s["id"] and set(e["gpus"]) & set(s["gpus"]):
            occ = "" if e.get("occupancy") == "live" else ", occupancy unknown"
            waits.append(f"entry {e['id']} ({e['user']}, {e['state']}{occ})")
    ok, idx, apps, _ = nvidia_view()
    if not ok:
        waits.append("nvidia-smi failed")
    else:
        missing = [g for g in s["gpus"] if not idx.get(g)]
        if missing:
            waits.append(f"GPUs {core.gpu_text(missing)} not identified by nvidia-smi")
        uuids = {idx.get(g) for g in s["gpus"] if idx.get(g)}
        for uuid, pid in apps:
            if uuid in uuids:
                uid = core.proc_uid(pid)
                waits.append(f"process {pid} ({core.owner_name(uid) if uid is not None else '?'})")
    return waits


# ------------------------------------------------------------------------------------------------ starting a run

LEADER_SIGNALS = ["SIGHUP", "SIGINT", "SIGQUIT", "SIGILL", "SIGTRAP", "SIGABRT", "SIGBUS", "SIGFPE", "SIGKILL", "SIGUSR1",
                  "SIGSEGV", "SIGUSR2", "SIGPIPE", "SIGALRM", "SIGTERM", "SIGXCPU", "SIGXFSZ", "SIGSYS"]


def start_main_unit(rid, s):
    u = s["units"]
    props = ["-p", "Type=exec", "-p", "RemainAfterExit=yes", "-p", f"WorkingDirectory={s['cwd']}",
             "-p", f"StandardOutput=append:{s['log']}", "-p", f"StandardError=append:{s['log']}",
             "-p", "KillMode=control-group", "-p", "TimeoutStopSec=10",
             # a leader exiting nonzero or by a signal would fail the unit, and systemd would then kill everything
             # left in its cgroup: every status counts as success, so the release rule decides (ExecMainStatus keeps
             # the real one)
             "-p", "SuccessExitStatus=" + " ".join([str(i) for i in range(1, 256)] + LEADER_SIGNALS)]
    if s.get("mem_max"):
        props += ["-p", f"MemoryMax={int(s['mem_max'])}M", "-p", "MemorySwapMax=0"]
    return systemd_run([f"--unit={u['main']}", f"--slice={u['slice']}", *props, *passthrough_env(), "--",
                        sys.executable, "-IB", own_bus(), "_job", rid])


def job_main(rid):
    """`bus _job <id>`: the main service's first process; becomes `bash -c '<command>'` with the saved environment."""
    spec = core.read_private(job_path(rid))
    if not spec:
        print(f"bus: no job spec for {rid}", file=sys.stderr)
        return 1
    os.chdir(spec["cwd"])
    os.execve("/bin/bash", ["bash", "-c", spec["command"]], spec["env"])


def classify(board, rows, gpus, scheduled, t):
    """Design 4.1 at launch: (class, booking row, protection text). Refuses what the class rules forbid."""
    if scheduled:
        b = next((r for r in rows if r["id"] == scheduled), None)
        if b is None:
            raise Refused(f"no booking {scheduled} on the board")
        if b["user"] != core.USER:
            raise Refused(f"{scheduled} is {b['user']}'s booking; --scheduled needs your own")
        if not set(gpus) <= set(b["gpus"]):
            raise Refused(f"{scheduled} covers GPUs {core.gpu_text(b['gpus'])}, not {core.gpu_text(gpus)}")
        if t >= b["end"]:
            raise Refused(f"{scheduled} ended at {time.strftime('%m-%d %H:%M', time.localtime(b['end']))}")
    other = bk.standing_active_on(rows, gpus, t, exclude=scheduled)
    if other:
        o = other[0]
        raise Refused(f"GPUs {core.gpu_text(sorted(set(o['gpus']) & set(gpus)))} are inside {o['user']}'s standing "
                      f"booking {o['id']} until {time.strftime('%m-%d %H:%M', time.localtime(o['end']))}")
    if not scheduled:
        return "backfill", None, "backfill: preemptible by any authorized request and by any standing booking's start"
    b = next(r for r in rows if r["id"] == scheduled)
    frm = max(b["start"], b.get("objection_close") or b["start"])
    if b.get("state") == "standing" and frm <= t:
        text = f"protected until {time.strftime('%m-%d %H:%M %Z', time.localtime(b['end']))} (booking standing)"
    elif b.get("state") in ("pending", "standing"):
        text = (f"protected from {time.strftime('%m-%d %H:%M', time.localtime(frm))} to "
                f"{time.strftime('%m-%d %H:%M %Z', time.localtime(b['end']))} if the booking stands; backfill until then")
    else:
        text = f"not protected: booking {b['id']} is {b.get('state')} ({b.get('why')}); runs as backfill"
    return "scheduled", b, text


def cmd_run(a):
    conf = core.load_conf()
    board = core.Board.from_conf(conf)
    try:
        board.writable()
        if board.host and board.host not in (core.HOST, os.uname().nodename) and not os.environ.get("AGENT_BUS_ANY_HOST"):
            raise Refused(f"runs start on {board.host} (the board's host); this is {core.HOST}")
        gpus = core.parse_gpus(a.gpus, board.ngpus)
        if a.kind not in core.KINDS:
            raise Refused(f"kind must be one of {', '.join(core.KINDS)}")
        if a.note and not core.note_ok(a.note):
            raise Refused(f"note: at most {core.NOTE_MAX} characters")
        t = now()
        entries = bk.entries_with_occupancy(board)
        rows = bk.evaluate(board, t, entries=entries)
        cls, b, prot_text = classify(board, rows, gpus, a.scheduled, t)
        systemd = user_manager_ok()
        if a.exclusive and cls == "scheduled" and not systemd:
            raise Refused("an exclusive scheduled launch needs a user manager (bus doctor)")
        rid = core.new_id(a.slug or a.note or "run", t)
        inbox = board.wdir("inbox")
        for n in range(2, 50):
            if core.read_file(board.wdir("agents"), f"{rid}.json") is None and not os.path.exists(state_path(rid)):
                fd = core.mkdir_own(inbox, rid, 0o3775)
                if fd is not None:
                    os.close(fd)
                    break
            rid = core.new_id(f"{a.slug or a.note or 'run'}-{n}", t)
        else:
            raise Refused("could not find a free entry id")
        until = b["end"] if cls == "scheduled" else t + int(float(a.hours or 24) * 3600)
        if a.hours and cls == "scheduled":
            until = min(b["end"], t + int(float(a.hours) * 3600))
        gen = secrets.token_hex(16)
        log = os.path.abspath(a.log) if a.log else os.path.join(runs_dir(), f"{rid}.log")
        cwd = os.path.abspath(a.cwd or os.getcwd())
        env = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES=",".join(map(str, gpus)),
                   AGENT_BUS_ID=rid, AGENT_BUS_RUN=gen)
        core.write_private(job_path(rid), dict(command=a.command, cwd=cwd, env=env, log=log))
        un = core.unit_name(rid)
        handoff = bool(a.exclusive and cls == "scheduled")
        s = dict(state_version=STATE_VERSION, id=rid, run=gen, host=core.HOST, user=core.USER,
                 board=dict(path=board.path, inode=board.ino, fsid=board.fsid), state="starting",
                 entry_state="waiting-handoff" if handoff else "running", end_reason=None, exit_status=None,
                 mode="systemd" if systemd else "fallback", handoff=handoff,
                 units=dict(slice=f"{un}.slice", main=f"{un}-main.service", sup=f"{un}-sup.service", cgroup_path=None),
                 **{"class": cls}, booking=b["id"] if b else None, booking_revision=b["revision"] if b else None,
                 gpus=gpus, started=t, until=until, kind=a.kind,
                 exclusive=bool(a.exclusive), note=a.note or "", stop_cost_min=a.stop_cost_min, cwd=cwd, log=log,
                 mem_max=a.mem_max, on_message=os.path.abspath(a.on_message) if a.on_message else None,
                 checkpoint_dir=a.checkpoint_dir, requests={}, release=None, lapsed=[],
                 session=os.environ.get("CLAUDE_CODE_SESSION_ID"), supervisor=None, notices=[])
        core.write_private(state_path(rid), s)
        admit_write(rid, dict(run=gen, admission="handoff" if handoff else "open", inflight=[]))
        if os.path.exists(log) is False:
            open(log, "a").close()
            os.chmod(log, 0o600)
    finally:
        board.close()
    print(f"bus: {prot_text}", file=sys.stderr)
    if not systemd:
        return run_fallback(rid, a)
    if not handoff:
        rc, out = start_main_unit(rid, s)
        if rc:
            s.update(state="ended", end_reason=f"the job's unit did not start: {out.strip()[:200]}")
            core.write_private(state_path(rid), s)
            raise Refused(f"systemd-run for the job failed: {out.strip()[:300]}")
        s["units"]["cgroup_path"] = cgroup_of(s["units"]["slice"], tries=5)
        s["main_started"] = now()
        core.write_private(state_path(rid), s)
    rc, out = systemd_run([f"--unit={s['units']['sup']}", "-p", "Type=exec", *passthrough_env(), "--",
                           sys.executable, "-IB", own_bus(), "supervise", rid])
    if rc:
        sysctl("stop", s["units"]["slice"])
        s.update(state="ended", end_reason=f"the supervisor did not start: {out.strip()[:200]}")
        core.write_private(state_path(rid), s)
        raise Refused(f"systemd-run for the supervisor failed: {out.strip()[:300]}")
    info = dict(id=rid, run=gen, state_file=state_path(rid), log=log, mode="systemd", slice=s["units"]["slice"],
                main_unit=s["units"]["main"], **{"class": cls}, booking=s["booking"])
    if a.info_file:
        core.write_private(os.path.abspath(a.info_file), info)
    print(f"bus: entry {rid} run {gen}", file=sys.stderr)
    if handoff:
        print(f"bus: exclusive: waiting for the handoff of GPUs {core.gpu_text(gpus)} (state waiting-handoff)",
              file=sys.stderr)
    if a.detach:
        print(json.dumps(info))
        return 0
    return wait_ended(rid, report_handoff=handoff)


def wait_ended(rid, report_handoff=False):
    """The foreground wait: until the private state is ended; exits with the job's status."""
    said = None
    while True:
        s = core.read_private(state_path(rid)) or {}
        if s.get("state") == "ended":
            if s.get("ended_by_release") or (s.get("exit_status") is None and s.get("release")):
                return EXIT_RELEASED
            st = s.get("exit_status")
            return st if isinstance(st, int) and st >= 0 else 1
        if report_handoff and s.get("handoff_waits") != said and s.get("state") == "waiting-handoff":
            said = s.get("handoff_waits")
            print(f"bus: waiting for: {'; '.join(said or []) or 'checking'}", file=sys.stderr)
        time.sleep(1)


def run_fallback(rid, a):
    """No user manager: the client forks the job in a new session and process group and supervises it itself."""
    s = load_state(rid)
    spec = core.read_private(job_path(rid))
    logfd = os.open(spec["log"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            os.dup2(logfd, 1)
            os.dup2(logfd, 2)
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.chdir(spec["cwd"])
            os.execve("/bin/bash", ["bash", "-c", spec["command"]], spec["env"])
        finally:
            os._exit(127)
    os.close(logfd)
    s.update(state="running", entry_state="running", pgid=pid, leader=core.proc_identity(pid), main_started=now())
    core.write_private(state_path(rid), s)
    info = dict(id=rid, run=s["run"], state_file=state_path(rid), log=spec["log"], mode="fallback", slice=None,
                main_unit=None, **{"class": s["class"]}, booking=s["booking"])
    if a.info_file:
        core.write_private(os.path.abspath(a.info_file), info)
    print(f"bus: entry {rid} run {s['run']} (fallback mode: this process supervises; killing it orphans the job)",
          file=sys.stderr)
    sup = Supervisor(rid, fallback_child=(pid, pid, None))
    sup.run()
    return wait_ended(rid)


# ------------------------------------------------------------------------------------------------ owner commands

def cmd_release(rid, reason=None, wait=None):
    s = load_state(rid)
    if s["state"] == "ended":
        return dict(id=rid, state="ended", released=True, remains=[])
    d = ctl_dir(rid)
    name = f"release-{time.time():.3f}-{secrets.token_hex(4)}.json"
    tmp = os.path.join(d, "." + name)
    with open(tmp, "w") as f:
        json.dump(dict(reason=reason or "released by its owner", t=now()), f)
    os.link(tmp, os.path.join(d, name))
    os.unlink(tmp)
    end = time.monotonic() + (wait or 0)
    while True:
        s = load_state(rid)
        if s["state"] == "ended" or time.monotonic() >= end:
            r = s.get("release") or {}
            return dict(id=rid, state=s["state"], released=s["state"] == "ended", remains=r.get("remains") or [])
        time.sleep(1)


def cmd_reconcile(rid, confirm):
    """The owner's word on processes of theirs a release cannot attribute: list them, or confirm pids that are not this
    run's (the supervisor then leaves them out). Stopping them instead also completes the release."""
    s = load_state(rid)
    clear, mine, other, err = gpu_release_check(rid, s["gpus"], set(s.get("reconciled", [])))
    if confirm:
        bad = [p for p in confirm if int(p) not in mine]
        if bad:
            raise Refused(f"{', '.join(map(str, bad))}: not among the processes waiting on this run's reconciliation")
        core.write_private(os.path.join(ctl_dir(rid), f"reconcile-{time.time():.3f}.json"),
                           dict(pids=[int(x) for x in confirm]))
    return dict(id=rid, state=s.get("state"), candidates=mine, other=other,
                confirmed=sorted(set(s.get("reconciled", [])) | set(int(x) for x in confirm or [])), error=err)


def cmd_adopt_all():
    """Start a supervisor for every nonterminal run of this host whose supervisor lock is free."""
    out = []
    if not user_manager_ok():
        return [dict(error="no user manager: adoption needs one")]
    for name in sorted(os.listdir(runs_dir())):
        if not name.endswith(".json") or name.endswith((".job.json", ".admit.json")):
            continue
        rid = name[:-5]
        s = core.read_private(os.path.join(runs_dir(), name)) or {}
        if s.get("state") not in NONTERMINAL or s.get("host") != core.HOST or s.get("mode") != "systemd":
            continue
        if not lock_free(os.path.join(runs_dir(), f"{rid}.lock")):
            continue
        unit = f"{core.unit_name(rid)}-sup-a{int(time.time())}{secrets.token_hex(2)}.service"
        rc, msg = systemd_run([f"--unit={unit}", "-p", "Type=exec", *passthrough_env(), "--", sys.executable, "-IB",
                               os.path.realpath(core.BIN()) if os.path.exists(core.BIN()) else own_bus(),
                               "supervise", rid, "--adopt"])
        out.append(dict(id=rid, unit=unit, started=rc == 0, detail=msg.strip()[:200]))
    return out


def cmd_runs():
    out = []
    for name in sorted(os.listdir(runs_dir())):
        if not name.endswith(".json") or name.endswith((".job.json", ".admit.json")):
            continue
        s = core.read_private(os.path.join(runs_dir(), name)) or {}
        if "id" not in s:
            continue
        out.append({k: s.get(k) for k in ("id", "run", "state", "class", "booking", "gpus", "started", "until",
                                          "end_reason", "exit_status", "host", "mode", "kind")})
    return out


def cmd_state(rid):
    s = load_state(rid)
    a = admit_read(rid)
    out = {k: v for k, v in s.items() if not k.startswith("_")}
    out["admission"] = a.get("admission")
    out["inflight"] = a.get("inflight")
    out["supervisor_lock_free"] = lock_free(os.path.join(runs_dir(), f"{rid}.lock"))
    return out
