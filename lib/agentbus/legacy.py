"""`bus legacy`: the old (bff1824-format) board, spoken from a private install with confined file operations.

Writes: message files (temp O_EXCL 0600, fchmod 0644, published by link); one's own entry files (temp and rename); one's
own heartbeat (touch through an O_NOFOLLOW descriptor); archiving one's own entries; appending to log.jsonl. mkdir
only for an inbox that does not exist when sending to it, mode 2775, never chmodded afterwards. No chmod of any
existing path. Everything read from the old board is untrusted data: old-board files are group-writable, so their
contents authenticate nothing. A stop goes only through a private identity record (legacy/<entry>.json:
{entry, pid, boot_id, ticks}) and a pidfd.
"""
import json, os, secrets, signal, stat, time

from . import core
from .core import Refused, now

OLD_STALE = 60       # bff1824: seconds without a heartbeat before an entry is unattended
RESPOND = 300        # bff1824: seconds a watched entry has to ack a request
OWN_STALE_S = 120    # sweep-own: an own entry whose heartbeat is this old is archived
LEGACY_ID = core.re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


def board_path(arg=None):
    p = arg or os.environ.get("AGENT_BUS_LEGACY_BOARD") or core.load_conf().get("legacy_path")
    if not p:
        raise Refused("no old board given: pass --board <dir> (or install.sh --legacy-board <dir>)")
    return p


class Old:
    """The old board, walked and pinned like v2 (pin from board.conf's legacy_inode/legacy_fsid when recorded)."""

    def __init__(self, path=None):
        self.path = board_path(path)
        conf = core.load_conf()
        self.fd = core.walk(self.path)
        self.ino, self.fsid = core.identity(self.fd)
        pin = (conf.get("legacy_inode"), conf.get("legacy_fsid")) if conf.get("legacy_path") == self.path else None
        self.ok = pin is None or not pin[0] or (int(pin[0]) == self.ino and int(pin[1]) == self.fsid)
        self._d = {}

    def close(self):
        for fd in self._d.values():
            if fd is not None:
                os.close(fd)
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def dir(self, *parts):
        if not parts:
            return self.fd
        key = "/".join(parts)
        if key not in self._d:
            fd, owned = self.fd, False
            for p in parts:
                try:
                    nfd = core.sub(fd, p)
                except OSError:
                    nfd = None
                if owned:
                    os.close(fd)
                if nfd is None:
                    fd, owned = None, False
                    break
                fd, owned = nfd, True
            self._d[key] = fd if owned else None
        return self._d[key]

    def writable(self):
        if not self.ok:
            raise Refused(f"old board identity changed at {self.path}: nothing is written there")

    def wdir(self, *parts):
        self.writable()
        fd = self.dir(*parts)
        if fd is None:
            raise Refused(f"{'/'.join(parts)} missing on the old board")
        return fd

    # --- reads (untrusted)

    def read(self, sub_dir, name):
        fd = self.dir(*sub_dir.split("/")) if sub_dir else self.fd
        if fd is None:
            return None
        return core.read_json(fd, name)

    def entries(self):
        fd = self.dir("agents")
        if fd is None:
            return []
        names, _ = core.scan(fd)
        out = []
        for name in names or []:
            if not name.endswith(".json"):
                continue
            r = core.read_json(fd, name)
            if r is None:
                continue
            a, st, _ = r
            if not isinstance(a.get("id"), str) or a["id"] != name[:-5] or not LEGACY_ID.fullmatch(a["id"]):
                continue
            a = dict(a, _owner=core.owner_name(st.st_uid), _mtime=st.st_mtime)
            out.append(a)
        return out

    def hb_mtime(self, aid, owner=None):
        fd = self.dir("agents", ".heartbeat")
        if fd is None:
            return None
        try:
            st = os.stat(aid, dir_fd=fd, follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or (owner is not None and st.st_uid != owner):
            return None
        return st.st_mtime

    def attended(self, a):
        last = self.hb_mtime(a["id"]) or 0
        hb = a.get("heartbeat") if isinstance(a.get("heartbeat"), (int, float)) else 0
        return now() - max(last, hb) <= OLD_STALE

    def state(self, a):
        """bff1824's state(): exited / unattended / expired / live. A pid is checked only for one's own records."""
        if not self.attended(a):
            return "unattended"
        u = a.get("until")
        return "expired" if isinstance(u, (int, float)) and u < now() else "live"

    def msgs(self, aid):
        out = []
        for box in (aid, "all"):
            fd = self.dir("inbox", box)
            if fd is None:
                continue
            names, _ = core.scan(fd)
            for name in names or []:
                if not name.endswith(".json"):
                    continue
                r = core.read_json(fd, name)
                if r is None:
                    continue
                m = r[0]
                if not isinstance(m.get("id"), str) or not isinstance(m.get("ts"), (int, float)) or m.get("from") == aid:
                    continue
                out.append(dict(m, _owner=core.owner_name(r[1].st_uid)))
        return sorted(out, key=lambda m: m["ts"])

    def unread(self, a):
        seen = set(a.get("seen") or [])
        started = a.get("started") if isinstance(a.get("started"), (int, float)) else 0
        return [m for m in self.msgs(a["id"]) if m["id"] not in seen and m["ts"] >= started]

    def bookings(self):
        fd = self.dir("bookings")
        if fd is None:
            return []
        names, _ = core.scan(fd)
        out = []
        for name in names or []:
            if name.endswith(".json"):
                r = core.read_json(fd, name)
                if r and isinstance(r[0].get("start"), int) and isinstance(r[0].get("end"), int) \
                        and isinstance(r[0].get("gpus"), list) and r[0].get("id") == name[:-5]:
                    out.append(dict(r[0], _owner=core.owner_name(r[1].st_uid)))
        return sorted(out, key=lambda b: (b["start"], b["id"]))

    # --- writes (confined)

    def event(self, action, **kw):
        try:
            core.append(self.wdir(), "log.jsonl", json.dumps(dict(ts=now(), user=core.USER, action=action, **kw),
                                                          ensure_ascii=False), mode=0o664, own_only=False)
        except (OSError, Refused):
            pass

    def wdir_root(self):
        self.writable()
        return self.fd

    def own_entry(self, aid):
        """(entry, fd of agents/) when agents/<aid>.json is ours, else Refused."""
        fd = self.wdir("agents")
        r = core.read_json(fd, f"{aid}.json")
        if r is None:
            raise Refused(f"no entry {aid} on the old board")
        if r[1].st_uid != core.UID:
            raise Refused(f"{aid} belongs to {core.owner_name(r[1].st_uid)}; only its owner changes it")
        return r[0], fd

    def write_entry(self, aid, a):
        fd = self.wdir("agents")
        try:
            st = os.stat(f"{aid}.json", dir_fd=fd, follow_symlinks=False)
            if st.st_uid != core.UID:
                raise Refused(f"{aid} belongs to {core.owner_name(st.st_uid)}")
        except FileNotFoundError:
            pass
        core.replace(fd, f"{aid}.json", json.dumps(a, indent=1, ensure_ascii=False).encode(), mode=0o644)

    def beat(self, aid):
        fd = self.wdir("agents", ".heartbeat")
        if not core.touch(fd, aid):
            raise Refused(f"heartbeat {aid} on the old board is not a regular file of ours; not touched")

    def inbox_dir(self, box):
        """The inbox directory, created (mode 2775, once) when it does not exist yet; never chmodded afterwards."""
        ifd = self.wdir("inbox")
        fd = core.sub(ifd, box)
        if fd is None:
            created = False
            try:
                os.mkdir(box, 0o2775, dir_fd=ifd)
                created = True
            except FileExistsError:
                pass
            fd = core.sub(ifd, box)
            if fd is None:
                raise Refused(f"inbox/{box} is not a directory")
            st = os.fstat(fd)
            if created and st.st_uid == core.UID and stat.S_IMODE(st.st_mode) != 0o2775:
                os.fchmod(fd, 0o2775)   # only the directory just created: the umask stripped its group write
        self._d[f"inbox/{box}"] = fd
        return fd

    def send(self, to, text, sender=None, kind="msg", reply_to=None, gpus=None, minutes=None):
        if not LEGACY_ID.fullmatch(to):
            raise Refused(f"bad recipient {to!r}")
        if kind != "reply" and to != "all" and self.read("agents", f"{to}.json") is None and self.dir("inbox", to) is None:
            raise Refused(f"no agent or inbox {to} on the old board")
        mid = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        m = dict(id=mid, ts=now(), from_user=core.USER, to=to, kind=kind, text=text, reply_to=reply_to, gpus=gpus,
                 minutes=minutes)
        m["from"] = sender or core.USER
        fd = self.inbox_dir(to)
        core.publish(fd, f"{mid}.json", json.dumps(m, indent=1, ensure_ascii=False).encode(), mode=0o644)
        self.event("send", msg=mid, frm=m["from"], to=to, kind=kind, text=text[:200])
        return mid

    def archive(self, aid, a, reason):
        """Move one's own entry to archive/ with when and why it went down; remove its heartbeat."""
        afd, gfd = self.wdir("agents"), self.wdir("archive")
        st = os.stat(f"{aid}.json", dir_fd=afd, follow_symlinks=False)
        if st.st_uid != core.UID:
            return False
        dest = f"{aid}-{now()}.json"
        try:
            os.rename(f"{aid}.json", dest, src_dir_fd=afd, dst_dir_fd=gfd)
        except FileNotFoundError:
            return False
        core.replace(gfd, dest, json.dumps(dict(a, down_at=now(), down_reason=reason), indent=1,
                                           ensure_ascii=False).encode(), mode=0o644)
        hb = self.dir("agents", ".heartbeat")
        if hb is not None:
            core.unlink_own(hb, aid)
        return True


def ident_path(aid):
    return os.path.join(core.private_dir("legacy"), f"{aid}.json")


# ------------------------------------------------------------------------------------------------ commands

def up(o, aid, gpus, task, hours, pid, on_request="agent", exclusive=False, human=None, session=None, host=None):
    if not LEGACY_ID.fullmatch(aid) or not aid.startswith(core.USER + "-"):
        raise Refused(f"entry ids start with {core.USER}-")
    old = {}
    r = o.read("agents", f"{aid}.json")
    if r is not None:
        if r[1].st_uid != core.UID:
            raise Refused(f"{aid} belongs to {core.owner_name(r[1].st_uid)}")
        old = r[0]
    a = dict(old, id=aid, user=core.USER, human=human or old.get("human") or core.USER,
             host=host or old.get("host") or core.HOST, gpus=gpus if gpus is not None else old.get("gpus", ""),
             task=task or old.get("task", ""), pid=pid or old.get("pid"),
             until=now() + int(hours * 3600) if hours else old.get("until"),
             on_request=on_request or old.get("on_request", "agent"),
             exclusive=bool(exclusive) if exclusive is not None else old.get("exclusive", False),
             started=old.get("started", now()), heartbeat=now(), seen=old.get("seen", []),
             session=session or os.environ.get("CLAUDE_CODE_SESSION_ID") or old.get("session"))
    if pid:
        ident = core.proc_identity(pid)
        if ident is None:
            raise Refused(f"pid {pid} is not running here: no identity record, no entry")
        core.write_private(ident_path(aid), dict(entry=aid, **ident, host=core.HOST, recorded=now()))
    o.write_entry(aid, a)
    o.beat(aid)
    o.event("up", agent=aid, gpus=a["gpus"], host=a["host"], task=a["task"], until=a["until"],
            on_request=a["on_request"], exclusive=a["exclusive"], pid=a["pid"])
    return a


def down(o, aid, reason="done"):
    a, _ = o.own_entry(aid)
    o.archive(aid, a, reason)
    o.event("down", agent=aid, reason=reason)
    try:
        os.unlink(ident_path(aid))
    except FileNotFoundError:
        pass


def ack(o, aid, msg_ids):
    a, _ = o.own_entry(aid)
    a["seen"] = sorted(set(a.get("seen") or []) | set(msg_ids))
    o.write_entry(aid, a)
    o.event("ack", agent=aid, msgs=list(msg_ids))


def ls_json(o, mine=False):
    out = []
    for a in o.entries():
        if mine and a["_owner"] != core.USER:
            continue
        row = {k: v for k, v in a.items() if not k.startswith("_")}
        row["state"] = o.state(a)
        row["file_owner"] = a["_owner"]
        row["identity_record"] = os.path.exists(ident_path(a["id"])) if a["_owner"] == core.USER else None
        out.append(row)
    return out


def cal_json(o, days=2):
    t0 = now()
    t1 = t0 + int(days * 86400)
    bs = [dict({k: v for k, v in b.items() if not k.startswith("_")}, file_owner=b["_owner"])
          for b in o.bookings() if b["end"] > t0 and b["start"] < t1]
    return dict(board=o.path, now=t0, until=t1, bookings=bs, entries=ls_json(o))


def signal_by_record(aid, sig=signal.SIGTERM):
    """Signal the process bound to an own entry by its private identity record, through a pidfd, only while the
    record's identity still matches. Returns (signalled, why)."""
    rec = core.read_private(ident_path(aid))
    if not rec:
        return False, "no private identity record: advisory only"
    if rec.get("host") not in (None, core.HOST):
        return False, f"recorded on {rec.get('host')}, not here"
    if not core.identity_alive(rec):
        return False, f"pid {rec.get('pid')} is no longer the recorded process"
    try:
        fd = os.pidfd_open(rec["pid"])
    except (OSError, AttributeError) as e:
        return False, f"pidfd_open failed: {e!r}"
    try:
        if not core.identity_alive(rec):   # the pidfd now pins the process: check it is still the recorded one
            return False, "the process changed while opening its pidfd"
        signal.pidfd_send_signal(fd, sig)
        return True, f"signalled pid {rec['pid']}"
    except OSError as e:
        return False, repr(e)
    finally:
        os.close(fd)


def sweep_own(o, reply=False):
    """The repointed sweep (design 6.1 step 2): only this user's own old-board entries.

    - archives own stale entries: file owned by this user, own heartbeat 120 s old or missing, and no live process
      bound by an identity record;
    - an unanswered request to an own entry (no watcher for 60 s, or unacked for 300 s, as at bff1824) stops the
      task only through its identity record and a pidfd; an entry without a record is advisory: its unanswered
      requests go to legacy/unanswered.jsonl for the dispatcher, once each;
    - removes own ended bookings; touches .sweep/<user>@<host>."""
    o.writable()
    sw = o.dir(".sweep")
    if sw is not None:
        core.touch(sw, f"{core.USER}@{core.HOST}", mode=0o664)
    report = dict(archived=[], stopped=[], advisory=[], noted=[])
    bfd = o.dir("bookings")
    for b in o.bookings():
        if b["_owner"] == core.USER and b["end"] < now() and bfd is not None and core.unlink_own(bfd, f"{b['id']}.json"):
            o.event("unbook", booking=b["id"], owner=core.USER, reason="ended", by="sweep-own")
    unanswered_path = os.path.join(core.private_dir("legacy"), "unanswered.jsonl")
    noted = core.read_private(os.path.join(core.private_dir("legacy"), "noted.json")) or {}
    for a in o.entries():
        if a["_owner"] != core.USER:
            continue
        aid = a["id"]
        rec = core.read_private(ident_path(aid))
        alive = bool(rec) and core.identity_alive(rec)
        hb = o.hb_mtime(aid, owner=core.UID)
        if (hb is None or now() - hb >= OWN_STALE_S) and not alive:
            if o.archive(aid, a, "stale: own entry, heartbeat 120 s old, no live identity record (sweep-own)"):
                o.event("down", agent=aid, owner=core.USER, reason="stale", by="sweep-own")
                report["archived"].append(aid)
                if rec:
                    os.unlink(ident_path(aid))
            continue
        reqs = [m for m in o.unread(a) if m.get("kind") == "request" and m.get("to") == aid]
        if not reqs:
            continue
        watched = o.attended(a)
        oldest = now() - min(m["ts"] for m in reqs)
        why = "no watcher" if not watched else (f"no answer within {RESPOND}s" if oldest > RESPOND else None)
        if why is None:
            continue
        ids = sorted(m["id"] for m in reqs)
        policy = str(a.get("on_request", "agent"))
        if policy.startswith("keep"):
            continue
        if not rec:
            new = [i for i in ids if i not in noted.get(aid, [])]
            if new:
                with open(unanswered_path, "a") as f:
                    for m in reqs:
                        if m["id"] in new:
                            f.write(json.dumps(dict(t=now(), entry=aid, msg=m["id"], frm=m.get("from"), why=why,
                                                    gpus=m.get("gpus"), minutes=m.get("minutes"),
                                                    text=str(m.get("text", ""))[:200])) + "\n")
                noted[aid] = sorted(set(noted.get(aid, [])) | set(new))
                report["advisory"].append(dict(entry=aid, msgs=new, why=why))
            continue
        ok, detail = signal_by_record(aid)
        text = (f"{why}; task '{a.get('task')}' was stopped and gpus {a.get('gpus')} on {core.HOST} are free" if ok
                else f"{why}; could not stop it: {detail}")
        if reply:
            for m in reqs:
                frm = m.get("from")
                if isinstance(frm, str) and LEGACY_ID.fullmatch(frm):
                    try:
                        o.send(frm, text, sender=aid, kind="reply", reply_to=m["id"])
                    except (OSError, Refused):
                        pass
        a["seen"] = sorted(set(a.get("seen") or []) | set(ids))
        a["sweep_noted"] = ids
        o.write_entry(aid, {k: v for k, v in a.items() if not k.startswith("_")})
        o.event("sweep", agent=aid, stopped=ok, why=why, policy=policy, pid=a.get("pid"), gpus=a.get("gpus"),
                msgs=ids, replied=reply, text=text, by="sweep-own")
        report["stopped" if ok else "noted"].append(dict(entry=aid, detail=detail))
    core.write_private(os.path.join(core.private_dir("legacy"), "noted.json"), noted)
    return report
