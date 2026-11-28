"""Paths, settings, confined file operations, record shapes and the board reader shared by every `bus` command.

Every path on a board is reached by an openat walk from `/` with O_NOFOLLOW at each component, and every record is
read relative to the walked directory descriptor, so nothing a member puts on the board (a symlink, a FIFO, a huge
file, a record claiming someone else's name) can redirect a read or a write. Python 3.9, standard library only.
"""
import errno, hashlib, json, os, pwd, re, secrets, socket, stat, time

# ------------------------------------------------------------------------------------------------ settings

def env_path(name, default):
    return os.path.expanduser(os.environ.get(name) or default)


def env_num(name, default):
    v = os.environ.get(name)
    return type(default)(v) if v else default


STATE = lambda: env_path("AGENT_BUS_STATE", "~/.local/state/agent-bus")     # private: home 0700, files 0600
CONFIG = lambda: env_path("AGENT_BUS_CONFIG", "~/.config/agent-bus")
BIN = lambda: env_path("AGENT_BUS_BIN", "~/.local/bin/bus")                  # the installed entry point (a symlink)
BUILDS = lambda: env_path("AGENT_BUS_BUILDS", "~/.local/share/agent-bus/builds")
PREFIX = lambda: os.environ.get("AGENT_BUS_UNIT_PREFIX") or "agentbus"      # tests use their own, apart from real units
NVSMI = lambda: os.environ.get("AGENT_BUS_NVIDIA_SMI") or "nvidia-smi"

PASS_S = lambda: env_num("AGENT_BUS_PASS_S", 5.0)            # one supervisor pass
PASS_BUDGET_S = lambda: env_num("AGENT_BUS_PASS_BUDGET_S", 4.0)  # the board work of one pass
HB_STALE_S = lambda: env_num("AGENT_BUS_HB_STALE_S", 120)    # a heartbeat this old: "occupancy unknown"
TERM_GRACE_S = lambda: env_num("AGENT_BUS_TERM_GRACE_S", 15.0)
VERIFY_S = lambda: env_num("AGENT_BUS_VERIFY_S", 60.0)       # after the stop: how long a release keeps verifying fast
HOOK_TIMEOUT_S = lambda: env_num("AGENT_BUS_HOOK_TIMEOUT_S", 10.0)
SAMPLE_EVERY = lambda: env_num("AGENT_BUS_SAMPLE_EVERY", 24)  # passes between GPU samples (120 s)
IDLE_S = lambda: env_num("AGENT_BUS_IDLE_S", 1800)           # under 5% this long: idle backfill

MAX_RECORD = 64 * 1024
RACY_NS = 5 * 10 ** 9      # receipts changed this recently are always re-read (ctime granularity)
MAX_LISTING = 20000
NOTE_MAX, MSG_NOTE_MAX, REASON_MAX = 80, 200, 200
KINDS = ("training", "eval", "benchmark-timing", "serving", "interactive-dev", "other")
CLASSES = ("scheduled", "backfill")
ENTRY_STATES = ("running", "stopping", "leader-exited", "ended", "waiting-handoff", "needs-reconcile")
SCHEMA = 2

USER = pwd.getpwuid(os.getuid()).pw_name
UID = os.getuid()
HOST = socket.gethostname().split(".")[0]


class Refused(Exception):
    """A command refuses: printed as `bus: <message>`, exit 1."""


def now():
    return int(time.time())


# ------------------------------------------------------------------------------------------------ users and processes

_names = {}


def owner_name(uid):
    if uid not in _names:
        try:
            _names[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _names[uid] = f"uid{uid}"
    return _names[uid]


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return "no-proc"


def proc_ticks(pid):
    """The process's start time in clock ticks since boot (field 22 of /proc/<pid>/stat), or None when it is gone."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            data = f.read()
    except (OSError, ValueError):
        return None
    try:
        return int(data[data.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError):
        return None


def proc_identity(pid):
    t = proc_ticks(pid)
    return None if t is None else {"pid": int(pid), "boot_id": boot_id(), "ticks": t}


def proc_state(pid):
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            data = f.read()
        return data[data.rindex(")") + 2:].split()[0]
    except (OSError, ValueError, IndexError):
        return None


def identity_state(ident):
    """'alive' (the recorded process, running), 'gone' (positively: no such pid, another start time or boot, or a
    zombie), or 'unknown' (/proc could not be read). Decisions that would release something treat 'unknown' as alive;
    decisions that would signal or clear something require 'alive'."""
    if not isinstance(ident, dict) or not isinstance(ident.get("pid"), int) or ident.get("ticks") is None:
        return "unknown"
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot = f.read().strip()
    except OSError:
        return "unknown"                 # a boot id that cannot be read proves no reboot
    if ident.get("boot_id") != boot:
        return "gone"
    try:
        with open(f"/proc/{ident['pid']}/stat") as f:
            data = f.read()
    except FileNotFoundError:
        return "gone"
    except OSError:
        return "unknown"
    try:
        rest = data[data.rindex(")") + 2:].split()
        state, ticks = rest[0], int(rest[19])
    except (ValueError, IndexError):
        return "unknown"
    if ticks != ident["ticks"] or state in ("Z", "X"):
        return "gone"
    return "alive"


def identity_gone(ident):
    return identity_state(ident) == "gone"


def proc_owner(pid):
    """(uid, None) for a live process, (None, 'gone') when /proc has no such pid, (None, 'error') when it cannot be
    read: an error is never taken for an exit."""
    try:
        return os.stat(f"/proc/{int(pid)}").st_uid, None
    except FileNotFoundError:
        return None, "gone"
    except (OSError, ValueError):
        return None, "error"


def identity_alive(ident):
    """True while the process named by {pid, boot_id, ticks} is the same process that was recorded and has not
    exited (a zombie keeps its /proc entry and start time until reaped, and counts as gone)."""
    if not isinstance(ident, dict) or not isinstance(ident.get("pid"), int):
        return False
    return (ident.get("boot_id") == boot_id() and proc_ticks(ident["pid"]) == ident.get("ticks")
            and proc_state(ident["pid"]) not in ("Z", "X", None))


def proc_uid(pid):
    try:
        return os.stat(f"/proc/{int(pid)}").st_uid
    except (OSError, ValueError):
        return None


def proc_cgroup(pid):
    """The process's cgroup v2 path ('0::<path>'), or None when it cannot be read."""
    try:
        with open(f"/proc/{int(pid)}/cgroup") as f:
            for line in f:
                if line.startswith("0::"):
                    return line[3:].strip()
    except (OSError, ValueError):
        return None
    return None


# ------------------------------------------------------------------------------------------------ confined files

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def walk(path):
    """A directory descriptor for an absolute path, opened component by component from / with O_NOFOLLOW."""
    if not os.path.isabs(path):
        raise ValueError(f"not an absolute path: {path}")
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in [p for p in path.split("/") if p]:
            if part in (".", ".."):
                raise ValueError(f"bad path component in {path}")
            nfd = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def sub(fd, name):
    """A child directory of fd, no symlink followed; None when it does not exist."""
    try:
        return os.open(name, DIR_FLAGS, dir_fd=fd)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        if e.errno == errno.ELOOP:   # a symlink where a directory belongs
            return None
        raise


def identity(fd):
    st = os.fstat(fd)
    return st.st_ino, os.fstatvfs(fd).f_fsid


def scan(fd, limit=MAX_LISTING):
    """Names in a directory: dot names skipped. Over `limit` names: (None, count) and the caller reports it."""
    names = []
    dup = os.dup(fd)
    try:
        with os.scandir(dup) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                names.append(e.name)
                if len(names) > limit:
                    return None, len(names)
    finally:
        try:
            os.close(dup)
        except OSError:
            pass
    return sorted(names), len(names)


def read_file(dirfd, name, limit=MAX_RECORD):
    """(bytes, stat) of a regular file of at most `limit` bytes, or None (symlink, FIFO, too big, gone)."""
    try:
        fd = os.open(name, READ_FLAGS, dir_fd=dirfd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        chunks, n = [], 0
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            n += len(b)
            if n > limit:
                return None
            chunks.append(b)
        return b"".join(chunks), st
    finally:
        os.close(fd)


def read_json(dirfd, name, limit=MAX_RECORD):
    r = read_file(dirfd, name, limit)
    if r is None:
        return None
    try:
        obj = json.loads(r[0])
    except ValueError:
        return None
    return (obj, r[1], hashlib.sha256(r[0]).hexdigest()) if isinstance(obj, dict) else None


def dumps(obj):
    return (json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n").encode()


def _tmp(dirfd, data, mode=0o644):
    name = f".tmp-{secrets.token_hex(8)}"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600,
                 dir_fd=dirfd)
    try:
        os.write(fd, data)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    return name


def publish(dirfd, name, data, mode=0o644):
    """Publish a new file once: temp (O_EXCL, 0600, fchmod), then link to its name. FileExistsError if taken."""
    tmp = _tmp(dirfd, data, mode)
    try:
        os.link(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
    finally:
        os.unlink(tmp, dir_fd=dirfd)


def replace(dirfd, name, data, mode=0o644):
    """Rewrite one's own file: temp and rename (a symlink at the name is replaced, never followed)."""
    tmp = _tmp(dirfd, data, mode)
    try:
        os.rename(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except BaseException:
        os.unlink(tmp, dir_fd=dirfd)
        raise


def touch(dirfd, name, mode=0o644, data=None):
    """Refresh one's own regular file, updating its payload when supplied and changed."""
    access = os.O_WRONLY if data is None else os.O_RDWR
    flags = access | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    created = False
    try:
        fd = os.open(name, flags, dir_fd=dirfd)
    except FileNotFoundError:
        try:
            if data is not None:
                publish(dirfd, name, data, mode)
                return True
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dirfd)
            created = True
        except OSError:
            return False
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != UID:
            return False
        if data is not None and os.pread(fd, len(data) + 1, 0) != data:
            replace(dirfd, name, data, mode)
            return True
        if created:
            os.fchmod(fd, mode)
        os.utime(fd)
        return True
    finally:
        os.close(fd)


def append(dirfd, name, line, mode=0o644, own_only=True):
    """Append one line through O_APPEND | O_NOFOLLOW; a shared log (the old board's) need not be ours."""
    fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
                 | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=dirfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (own_only and st.st_uid != UID):
            raise PermissionError(f"{name} is not our own regular file")
        os.write(fd, (line.rstrip("\n") + "\n").encode())
    finally:
        os.close(fd)


def unlink_own(dirfd, name):
    """Remove a file of ours (checked through lstat at the directory descriptor). False when absent or not ours."""
    try:
        st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if st.st_uid != UID:
        return False
    try:
        os.unlink(name, dir_fd=dirfd)
        return True
    except FileNotFoundError:
        return False


def mkdir_own(dirfd, name, mode):
    """Create a directory of ours and set its mode through its own descriptor. Returns its fd, or None when the name
    is taken by someone else's directory (the caller picks a new name)."""
    try:
        os.mkdir(name, 0o700, dir_fd=dirfd)
        created = True
    except FileExistsError:
        created = False
    fd = sub(dirfd, name)
    if fd is None:
        return None
    st = os.fstat(fd)
    if st.st_uid != UID:
        os.close(fd)
        return None
    if created:                        # an existing directory keeps its mode: nothing is chmodded after creation
        os.fchmod(fd, mode)
    return fd


# ------------------------------------------------------------------------------------------------ private state

def private_dir(*parts):
    """A directory under the private state root (0700), created as needed."""
    root = STATE()
    os.makedirs(root, mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    p = os.path.join(root, *parts)
    os.makedirs(p, mode=0o700, exist_ok=True)
    return p


def write_private(path, obj):
    d = os.path.dirname(path)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(dumps(obj))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_private(path):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read())
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------ install config

def load_conf(path=None):
    """~/.config/agent-bus/board.conf: key=value lines (path, inode, fsid, host)."""
    p = path or os.path.join(CONFIG(), "board.conf")
    out = {}
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def save_conf(conf, path=None):
    p = path or os.path.join(CONFIG(), "board.conf")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + f".{secrets.token_hex(4)}"
    with open(tmp, "w") as f:
        for k in sorted(conf):
            f.write(f"{k}={conf[k]}\n")
    os.replace(tmp, p)


def board_settings(text):
    """The board's own `config` (host=..., gpus=8, budget_gpu_h=96, ...): key=value, parsed, never executed."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


DEFAULT_LIMITS = dict(gpus=8, horizon_s=7 * 86400, max_len_s=24 * 3600, min_objection_s=600, budget_gpu_h=96.0,
                      budget_span_s=7 * 86400, lapse_s=1800, default_objection_s=900)


# ------------------------------------------------------------------------------------------------ record shapes

NAME = r"[a-z_][a-z0-9_.]{0,31}"
STAMP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
HEX32 = re.compile(r"[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")


def is_int(v):
    return type(v) is int


def component(v, what="id"):
    """A single, safe path component: refuses empty, '.', '..', separators, NUL and anything outside STAMP_ID."""
    if not isinstance(v, str) or v in (".", "..") or "/" in v or "\0" in v or STAMP_ID.fullmatch(v) is None:
        raise Refused(f"bad {what} {v!r}: one name of letters, digits, '.', '_' or '-'")
    return v


def owned_id(v, user):
    return isinstance(v, str) and STAMP_ID.fullmatch(v) is not None and v.startswith(user + "-") and len(v) > len(user) + 1


def note_ok(v, n=NOTE_MAX):
    return isinstance(v, str) and len(v) <= n and not any(ord(c) < 32 or ord(c) == 127 for c in v)


def gpus_ok(v, ngpus):
    return (isinstance(v, list) and v and all(is_int(g) and 0 <= g < ngpus for g in v) and len(set(v)) == len(v))


def check_entry(e, ngpus):
    return (e.get("schema") == SCHEMA and isinstance(e.get("user"), str) and owned_id(e.get("id"), e["user"])
            and isinstance(e.get("host"), str) and gpus_ok(e.get("gpus"), ngpus) and e.get("class") in CLASSES
            and (e.get("booking") is None or isinstance(e.get("booking"), str))
            and isinstance(e.get("run"), str) and HEX32.fullmatch(e["run"]) is not None
            and is_int(e.get("started")) and is_int(e.get("until")) and e.get("kind") in KINDS
            and isinstance(e.get("exclusive"), bool) and note_ok(e.get("note", ""))
            and e.get("state") in ENTRY_STATES
            and (e.get("stop_cost_min") is None or is_int(e.get("stop_cost_min"))))


def check_booking(b, ngpus):
    return (b.get("schema") == SCHEMA and isinstance(b.get("user"), str) and owned_id(b.get("id"), b["user"])
            and gpus_ok(b.get("gpus"), ngpus) and is_int(b.get("start")) and is_int(b.get("end"))
            and is_int(b.get("created")) and is_int(b.get("objection_s")) and b.get("kind") in KINDS
            and note_ok(b.get("note", "")) and b.get("source") in ("cli", "gpupool", "request")
            and (b.get("ref") is None or note_ok(b.get("ref"), 120)))


def check_objection(o):
    return (isinstance(o.get("user"), str) and owned_id(o.get("id"), o["user"]) and isinstance(o.get("booking"), str)
            and isinstance(o.get("booking_revision"), str) and HEX64.fullmatch(o["booking_revision"]) is not None
            and isinstance(o.get("reason"), str) and 1 <= len(o["reason"].strip()) and len(o["reason"]) <= REASON_MAX
            and is_int(o.get("created")))


def check_arrival(x):
    return (isinstance(x.get("user"), str) and owned_id(x.get("id"), x["user"]) and isinstance(x.get("entry"), str)
            and owned_id(x["entry"], x["user"]) and is_int(x.get("arrived")) and x.get("schema") == SCHEMA
            and isinstance(x.get("revision"), str) and HEX64.fullmatch(x["revision"]) is not None
            and x.get("key") == f"{x['id']}@{x['revision'][:16]}")


def check_receipt(r):
    return (is_int(r.get("slot")) and r["slot"] > 0 and r.get("kind") in ("booking", "objection")
            and isinstance(r.get("user"), str) and owned_id(r.get("id"), r["user"])
            and isinstance(r.get("revision"), str) and HEX64.fullmatch(r["revision"]) is not None
            and is_int(r.get("created")))


def check_message(m):
    if not (m.get("schema") == SCHEMA and isinstance(m.get("sender"), str) and owned_id(m.get("id"), m["sender"])
            and isinstance(m.get("to"), str) and m.get("kind") in ("request", "reply", "notice")
            and note_ok(m.get("note", ""), MSG_NOTE_MAX)):
        return False
    if m["kind"] == "request":
        return (isinstance(m.get("request_id"), str) and HEX32.fullmatch(m["request_id"]) is not None
                and isinstance(m.get("reply_to"), str) and owned_id(m["reply_to"], m["sender"])
                and (m.get("to_run") is None or isinstance(m.get("to_run"), str)))
    if m["kind"] == "reply":
        return (m.get("answer") in ("yes", "no") and (m["answer"] == "yes" or note_ok(m.get("reason") or "", REASON_MAX)
                                                      and bool((m.get("reason") or "").strip())))
    return True


# ------------------------------------------------------------------------------------------------ the board

class Board:
    """A v2 board reached by the openat walk and checked against the install's pin (inode, fsid).

    `ok` False means the board identity changed: reading still works (callers show it), and nothing may be written
    to the board or acted on because of what it holds."""

    def __init__(self, path, pin=None):
        self.path = path
        self.fd = walk(path)
        self.ino, self.fsid = identity(self.fd)
        st = os.fstat(self.fd)
        self.creator = st.st_uid       # the account that ran setup-board.sh owns the board's directories
        self.pin = pin
        self.ok = pin is None or (int(pin[0]) == self.ino and int(pin[1]) == self.fsid)
        self.warnings = []
        self._dirs = {}
        r = read_file(self.fd, "config", 4096)
        self.settings = board_settings(r[0].decode("utf-8", "replace")) if r else {}
        lim = dict(DEFAULT_LIMITS)
        for k in lim:
            if k in self.settings:
                try:
                    lim[k] = type(lim[k])(float(self.settings[k])) if isinstance(lim[k], int) else float(self.settings[k])
                except ValueError:
                    pass
        self.limits = lim
        self.ngpus = lim["gpus"]
        self.host = self.settings.get("host")

    @classmethod
    def from_conf(cls, conf=None, path=None):
        conf = conf if conf is not None else load_conf()
        p = path or conf.get("path")
        if not p:
            raise Refused("no board configured: run install.sh --board <dir> (or pass --board)")
        pin = (conf["inode"], conf["fsid"]) if conf.get("inode") and (path is None or path == conf.get("path")) else None
        return cls(p, pin)

    def close(self):
        for fd in self._dirs.values():
            if fd is not None:
                os.close(fd)
        self._dirs = {}
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def recheck(self):
        """Walk again and compare with the pin; a pass calls this first."""
        try:
            fd = walk(self.path)
        except OSError:
            self.ok = False
            return False
        ino, fsid = identity(fd)
        os.close(fd)
        if self.pin is not None:
            self.ok = int(self.pin[0]) == ino and int(self.pin[1]) == fsid and ino == self.ino
        else:
            self.ok = ino == self.ino and fsid == self.fsid
        return self.ok

    def dir(self, *parts):
        """A descriptor for a directory under the board (cached for this Board's life), or None when missing. Every
        part must be one plain name: no record field ever becomes a path here."""
        for p in parts:
            if not isinstance(p, str) or not p or p in (".", "..") or "/" in p or "\0" in p:
                raise Refused(f"bad name {p!r} on the board")
        key = "/".join(parts)
        if key not in self._dirs:
            fd, owned = self.fd, False
            for p in parts:
                try:
                    nfd = sub(fd, p)
                except OSError:      # a symlink or a non-directory where a directory belongs
                    nfd = None
                if owned:
                    os.close(fd)
                if nfd is None:
                    fd, owned = None, False
                    break
                fd, owned = nfd, True
            self._dirs[key] = fd if owned else None
        return self._dirs[key]

    def writable(self):
        if not self.ok:
            raise Refused(f"board identity changed at {self.path}: nothing is written there")

    # --- readers

    def records(self, sub_dir, check, owner_field="user", stem_field="id"):
        """Valid records of one directory: right shape, file owner equal to its user field, stem equal to its id."""
        fd = self.dir(*sub_dir.split("/"))
        if fd is None:
            return []
        names, n = scan(fd)
        if names is None:
            self.warnings.append(f"{sub_dir}: more than {MAX_LISTING} entries ({n}); skipped")
            return []
        out = []
        for name in names:
            if not name.endswith(".json"):
                continue
            r = read_json(fd, name)
            if r is None:
                continue
            obj, st, sha = r
            try:
                good = check(obj)
            except (TypeError, AttributeError):
                good = False
            if not good or owner_name(st.st_uid) != obj.get(owner_field) or str(obj.get(stem_field)) != name[:-5]:
                continue
            obj = dict(obj)
            obj["_mode"], obj["_sha"], obj["_mtime"] = stat.S_IMODE(st.st_mode), sha, st.st_mtime
            out.append(obj)
        return out

    def entries(self):
        return self.records("agents", lambda e: check_entry(e, self.ngpus))

    def heartbeat_age(self, e):
        fd = self.dir("agents", ".heartbeat")
        if fd is None:
            return None
        try:
            st = os.stat(e["id"], dir_fd=fd, follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or owner_name(st.st_uid) != e["user"]:
            return None
        return max(0, time.time() - st.st_mtime)

    def occupancy(self, e):
        age = self.heartbeat_age(e)
        return "live" if age is not None and age < HB_STALE_S() else "unknown"

    def bookings_raw(self):
        return self.records("bookings", lambda b: check_booking(b, self.ngpus))

    def objections(self):
        return self.records("objections", check_objection)

    def arrivals(self):
        return self.records("arrivals", check_arrival, stem_field="key")

    def receipts(self, cache=None):
        """{slot: receipt} from seq/<k>/<n>, only in blocks owned by the board's creator. `cache` (a dict) keeps
        parsed receipts across passes of one process, keyed by slot, inode, ctime and size, so a receipt rewritten in
        place is read again (and its new content then differs from what slots-seen recorded for it)."""
        seq = self.dir("seq")
        if seq is None:
            return {}
        out, empty_run = {}, 0
        for k in range(0, 1000):
            bfd = self.dir("seq", str(k))
            if bfd is None:
                break
            if os.fstat(bfd).st_uid != self.creator:
                self.warnings.append(f"seq/{k} is not owned by the board's creator; ignored")
                continue
            n = 0
            dup = os.dup(bfd)
            with os.scandir(dup) as it:
                ents = []
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:   # ctime moves on any write or chmod and cannot be set back by its owner
                        st = e.stat(follow_symlinks=False)
                        ents.append((e.name, (e.inode(), st.st_ctime_ns, st.st_size)))
                    except OSError:
                        continue
            try:
                os.close(dup)
            except OSError:
                pass
            for name, ino in ents:
                if not name.isdigit() or int(name) // 10000 != k:
                    continue
                slot = int(name)
                key = (slot, ino)
                # a receipt changed within RACY_NS of now may have kept its (inode, ctime, size) key on a coarse
                # clock: it is read and hashed again rather than trusted from the cache
                racy = time.time_ns() - ino[1] < RACY_NS
                if cache is not None and key in cache and not racy:
                    out[slot] = cache[key]
                    n += 1
                    continue
                r = read_json(bfd, name, 4096)
                if r is None:
                    continue
                obj, st, sha = r
                if not check_receipt(obj) or obj["slot"] != slot or owner_name(st.st_uid) != obj["user"]:
                    continue
                obj = dict(obj, _sha=sha)
                out[slot] = obj
                if cache is not None:
                    cache[key] = obj
                n += 1
            empty_run = empty_run + 1 if n == 0 else 0
            if empty_run >= 2:
                break
        return out

    def messages(self, box):
        """Valid messages in inbox/<box>/: owner equal to the sender field, stem equal to the id."""
        return self.records(f"inbox/{box}", check_message, owner_field="sender")

    def inbox_owner(self, box):
        try:
            component(box, "inbox")
        except Refused:
            return None
        fd = self.dir("inbox")
        if fd is None:
            return None
        try:
            st = os.stat(box, dir_fd=fd, follow_symlinks=False)
        except OSError:
            return None
        return owner_name(st.st_uid) if stat.S_ISDIR(st.st_mode) else None

    # --- writers (only after writable())

    def wdir(self, *parts):
        self.writable()
        fd = self.dir(*parts)
        if fd is None:
            raise Refused(f"{'/'.join(parts)} missing on the board (setup-board.sh creates it)")
        return fd

    def log(self, action, **kw):
        if not self.ok:
            return
        try:
            append(self.wdir("log"), f"{USER}.jsonl", json.dumps(dict(ts=now(), user=USER, action=action, **kw),
                                                               ensure_ascii=False, sort_keys=True))
        except (OSError, Refused):
            pass


def clean(rec):
    """A record without the reader's private annotations."""
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def new_id(slug, t=None):
    s = re.sub(r"[^a-z0-9]+", "-", (slug or "run").lower()).strip("-")[:30] or "run"
    return f"{USER}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(t or time.time()))}-{s}"


def unit_name(rid):
    """Systemd unit stem for a run id: hyphens would make nested slices, so they become underscores."""
    return f"{PREFIX()}-{rid.replace('-', '_')}"


def gpu_text(gpus):
    runs, out = [], []
    for g in sorted(gpus):
        if runs and g == runs[-1][1] + 1:
            runs[-1][1] = g
        else:
            runs.append([g, g])
    for lo, hi in runs:
        out.append(str(lo) if lo == hi else f"{lo}-{hi}")
    return ",".join(out)


def parse_gpus(text, ngpus=8):
    out = set()
    for part in str(text).replace(" ", "").split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        if not lo.isdigit() or (hi and not hi.isdigit()) or int(hi or lo) < int(lo) or int(hi or lo) >= ngpus:
            raise Refused(f"bad GPU list {text!r}: indices 0-{ngpus - 1}, like 0-3 or 1,3")
        out.update(range(int(lo), int(hi or lo) + 1))
    if not out:
        raise Refused("no GPUs given")
    return sorted(out)


def errno_name(e):
    return errno.errorcode.get(getattr(e, "errno", 0) or 0, type(e).__name__)


BUS_LINE = re.compile(r"GPU ([0-9A-Fa-f]{4,8}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-9A-Fa-f])\Z")


def gpu_report_buses(report):
    """The PCI bus ids an `nvidia-smi -q` report has a section for."""
    return {m.group(1).upper() for m in (BUS_LINE.fullmatch(line.strip()) for line in report.splitlines()) if m}


def graphics_apps(report):
    """[(bus id, pid, MiB)] for graphics-only processes (Type G, such as EGL rendering) in an `nvidia-smi -q -d PIDS`
    report. --query-compute-apps leaves them out, and this driver has no --query-graphics-apps."""
    out, bus, proc = [], None, None
    for line in report.splitlines():
        text = line.strip()
        head = BUS_LINE.fullmatch(text)
        if head:
            bus, proc = head.group(1).upper(), None
            continue
        key, sep, value = (part.strip() for part in text.partition(" : "))
        if not sep or bus is None:
            continue
        if key == "Process ID" and value.isdigit():
            proc = dict(bus=bus, pid=int(value), type="", mem=0)
            out.append(proc)
        elif proc is not None and key == "Type":
            proc["type"] = value
        elif proc is not None and key == "Used GPU Memory":
            proc["mem"] = int(value.split()[0]) if value.split() and value.split()[0].isdigit() else 0
    return [(p["bus"], p["pid"], p["mem"]) for p in out if p["type"] == "G"]
