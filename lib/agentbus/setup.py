"""setup-board.sh's work, done from pinned directory descriptors: the parent is reached by the openat walk with
O_NOFOLLOW, every directory is created and then opened relative to its parent's descriptor, and a mode or group is set
only on what this run created. A symlink or another account's directory at any level stops the setup, so nothing a
member puts in the group-writable parent can redirect it."""
import grp, os

from . import core
from .core import Refused

DIRS = ("agents", "agents/.heartbeat", "inbox", "bookings", "objections", "arrivals", "seq", "archive", "log")


def _dir(parent_fd, name, mode, gid, made, fixed=False):
    """Create (or open an existing directory of ours) `name` under parent_fd; returns its descriptor. `fixed` names
    come from DIRS or are block numbers, not from the caller."""
    if not fixed:
        core.component(name, "directory")
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    fd = core.sub(parent_fd, name)
    if fd is None:
        raise Refused(f"{name} exists and is not a directory (a symlink or a file): stopping")
    st = os.fstat(fd)
    if st.st_uid != core.UID:
        os.close(fd)
        raise Refused(f"{name} belongs to {core.owner_name(st.st_uid)}: stopping")
    if created:
        if gid is not None:
            os.fchown(fd, -1, gid)
        os.fchmod(fd, mode)
        made.append(name)
    return fd


def _file(dir_fd, name, data, replace_own=False):
    if replace_own:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if st.st_uid != core.UID:
                raise Refused(f"{name} belongs to {core.owner_name(st.st_uid)}: stopping")
        except FileNotFoundError:
            pass
        core.replace(dir_fd, name, data, 0o644)
        return
    try:
        core.publish(dir_fd, name, data, 0o644)
    except FileExistsError:
        pass


def setup_board(root, host=None, group=None, gpus=8, blocks=1000, add_blocks=None, readme=None):
    root = os.path.abspath(root)
    parent, name = os.path.dirname(root), os.path.basename(root)
    gid = grp.getgrnam(group).gr_gid if group else None
    made = []
    try:
        pfd = core.walk(parent)
    except OSError as e:
        raise Refused(f"{parent}: {core.errno_name(e)} (a symlink or a missing directory on the way): stopping") from None
    try:
        if add_blocks is not None:
            rfd = core.sub(pfd, core.component(name, "board directory"))
            if rfd is None or os.fstat(rfd).st_uid != core.UID:
                raise Refused(f"{root} is not a board of yours")
            sfd = core.sub(rfd, "seq")
            if sfd is None or core.read_file(rfd, "config", 4096) is None:
                raise Refused(f"{root} is not a v2 board")
            names, _ = core.scan(sfd)
            last = max([int(n) for n in names or [] if n.isdigit()], default=-1)
            for k in range(last + 1, last + 1 + add_blocks):
                os.close(_dir(sfd, str(k), 0o3775, gid, made, fixed=True))
            os.close(sfd)
            os.close(rfd)
            return dict(blocks=f"0-{last + add_blocks}", made=made)
        if not host or not all(c.isalnum() or c in ".-" for c in host):
            raise Refused("bad host name")
        rfd = _dir(pfd, core.component(name, "board directory"), 0o2755, gid, made)
        try:
            b = core.sub(rfd, "bin")
            if b is not None:
                old = core.read_file(b, "bus.py") is not None
                os.close(b)
                if old:
                    raise Refused(f"{root} holds an old (v1) board; a v2 board goes in its own directory")
            fds = {"": rfd}
            for d in DIRS:
                head, _, tail = d.rpartition("/")
                fds[d] = _dir(fds[head], tail, 0o3775, gid, made, fixed=True)
            for k in range(blocks):
                os.close(_dir(fds["seq"], str(k), 0o3775, gid, made, fixed=True))
            _file(rfd, "config", f"host={host}\ngpus={int(gpus)}\n".encode())
            if readme:
                _file(rfd, "README.md", readme, replace_own=True)
            for d, fd in fds.items():
                if d:
                    os.close(fd)
        finally:
            os.close(rfd)
        return dict(root=root, made=len(made), blocks=blocks)
    finally:
        os.close(pfd)
