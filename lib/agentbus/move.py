"""Archive an empty board and let confirmed clients follow its owner's move record."""
import fcntl
import os
import re
import stat

from . import core, setup
from .core import Refused

RECORD = "move.json"
SEAL = ".move-seal"
CLIENT_PREFIX = "account-build-"
MOVE_PROTOCOL = 2
PUBLISHER_LOOKBACK_S = 14 * 86400
RUN_INBOX = re.compile(r"\d{8}-\d{6}-.+")


def confirmed():
    from . import auto_upgrade
    return not auto_upgrade.read_state().get("pending")


def announce(board, build, *, idle=False):
    if not board.ok or board.pending or not confirmed():
        return
    try:
        data = core.dumps(dict(user=core.USER, build=build, move_protocol=MOVE_PROTOCOL, idle=idle))
        core.touch(board.wdir("agents", ".heartbeat"), CLIENT_PREFIX + core.USER + ".json", data=data)
    except (OSError, Refused):
        pass


def retired(pin):
    return f"{pin['path']}.retired-{int(pin['inode'])}"


def matches(board, pin):
    return board.ino == int(pin["inode"]) and board.fsid == int(pin["fsid"])


def owner_file(board, name, limit=4096):
    record = core.read_file(board.fd, name, limit)
    return record if record and record[1].st_uid == board.creator and not record[1].st_mode & 0o022 else None


def owned_json(board, name, limit=4096):
    record = core.read_json(board.fd, name, limit)
    return record[0] if record and record[1].st_uid == board.creator and not record[1].st_mode & 0o022 else None


def follow(pin):
    """Use the exact old inode and the owner-authenticated destination, even if the source name was reused."""
    if pin.get("host") != core.HOST or not confirmed():
        return None
    try:
        old = core.Board(retired(pin))
    except (OSError, ValueError):
        return None
    with old:
        record = owned_json(old, RECORD)
        if not matches(old, pin) or not record or record.get("host") != core.HOST or old.host != core.HOST:
            return None
        path = record.get("path")
        if (not isinstance(path, str) or not os.path.isabs(path) or path != path.strip()
                or any(c in path for c in "\0\r\n") or path == pin["path"]
                or not all(core.is_int(record.get(k)) for k in ("inode", "fsid"))):
            return None
        try:
            new = core.Board(path, (record["inode"], record["fsid"])).lease()
        except (OSError, ValueError):
            return None
        source = dict(path=pin["path"], inode=old.ino, fsid=old.fsid)
        if (new.ok and new.recheck() and new.creator == old.creator and new.host == core.HOST
                and new.limits == old.limits and not new.pending and not os.fstat(new.fd).st_mode & 0o022
                and owner_file(new, "config") and new.origin.get("source") == source):
            return new
        new.close()
        return None


def require_owner(board):
    if board.creator != core.UID or os.fstat(board.fd).st_mode & 0o022 or not owner_file(board, "config"):
        raise Refused(f"{board.path}: run this as the board owner with an owner-writable root and config")


def capable(row):
    build = row.get("build")
    return row.get("move_protocol") == MOVE_PROTOCOL and bool(build) and core.note_ok(build, 128)


def names_in(board, *parts):
    fd = board.dir(*parts)
    if fd is None:
        raise Refused(f"missing {'/'.join(parts)} on the source board")
    names, _ = core.scan(fd)
    if names is None:
        raise Refused(f"cannot enumerate {'/'.join(parts)} for cutover")
    return names


def publishers(board, entries, receipts, t):
    """Pair a run heartbeat with its entry. A newer legacy entry or heartbeat supersedes an earlier claim."""
    authors, versions, archived = set(), {}, set()

    def observe(user, when, build=None, is_capable=False):
        if when >= t - PUBLISHER_LOOKBACK_S:
            authors.add(user)
        prior = versions.get(user, (0, "", False))
        if build is not None and (when > prior[0] or when == prior[0] and not is_capable):
            versions[user] = when, build, is_capable

    heartbeat = board.dir("agents", ".heartbeat")
    for e in entries:
        observe(e["user"], e["_ctime"], e.get("build") or "unknown", capable(e))
        hb = core.read_file(heartbeat, e["id"], 4096) if heartbeat is not None else None
        if hb and core.owner_name(hb[1].st_uid) == e["user"] and not hb[1].st_mode & 0o022:
            build = hb[0].decode("utf-8", "replace").strip() or "unknown"
            observe(e["user"], hb[1].st_ctime, build, capable(e) and build == e.get("build"))

    archive = board.dir("archive")
    for name in names_in(board, "archive"):
        if not name.endswith(".json"):
            continue
        row = core.read_json(archive, name)
        if row:
            e, st, _ = row
            user = core.owner_name(st.st_uid)
            if (core.check_entry(e, board.ngpus) and e["user"] == user and not st.st_mode & 0o022
                    and re.fullmatch(re.escape(e["id"]) + r"-\d+\.json", name)):
                archived.add(e["id"])
                observe(user, st.st_ctime, e.get("build") or "unknown", capable(e))

    for e in [*board.bookings_raw(), *board.objections(), *board.arrivals(), *receipts.values()]:
        observe(e["user"], e["_ctime"], "unreported")
    for name in names_in(board, "log"):
        try:
            st = os.stat(name, dir_fd=board.dir("log"), follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode) and name == core.owner_name(st.st_uid) + ".jsonl":
            observe(core.owner_name(st.st_uid), st.st_ctime, "unreported")

    idle = {}
    for name in names_in(board, "agents", ".heartbeat"):
        if not name.startswith(CLIENT_PREFIX) or not name.endswith(".json"):
            continue
        row = core.read_json(heartbeat, name, 4096)
        if row:
            e, st, _ = row
            user = core.owner_name(st.st_uid)
            if (name == CLIENT_PREFIX + user + ".json" and e.get("user") == user and not st.st_mode & 0o022
                    and core.note_ok(e.get("build"), 128)):
                observe(user, st.st_ctime, e["build"], capable(e))
                if capable(e) and e.get("idle") is True:
                    idle[user] = st.st_ctime

    # Run admission creates the inbox while holding the board lease. An account's idle report can clear
    # earlier orphaned inboxes. A later inbox must wait for its own entry or the next idle report.
    run_ids = {e["id"] for e in entries} | archived
    unaccounted = []
    inbox = board.dir("inbox")
    for box in names_in(board, "inbox"):
        user = board.inbox_owner(box)
        if user is None:
            continue
        for m in board.messages(box):
            observe(m["sender"], m["_ctime"], "unreported")
        if box.startswith(user + "-") and RUN_INBOX.fullmatch(box[len(user) + 1:]) and box not in run_ids:
            try:
                st = os.stat(box, dir_fd=inbox, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if core.owner_name(st.st_uid) != user or st.st_mtime >= idle.get(user, 0):
                unaccounted.append(f"{box} ({user})")
    missing = [f"{user} ({versions.get(user, (0, 'unknown', False))[1]})" for user in sorted(authors)
               if not versions.get(user, (0, "", False))[2]]
    return missing, unaccounted


def require_empty(board):
    from . import bookings

    t = core.now()
    entries = bookings.entries_with_occupancy(board)
    receipts = board.receipts()
    # The board owner's existing observations may only push a live objection deadline later.
    seen = (core.read_private(bookings.seen_file(board)) or {}).get("slots", {})
    rows = bookings.evaluate(board, t, entries=entries, receipts=receipts, seen=seen, record=False)
    missing, unaccounted = publishers(board, entries, receipts, t)
    times = bookings.sequence_times(receipts, seen)
    open_windows = []
    for r in rows:
        close = r.get("objection_close")
        if close is not None:
            slot = r["order"]  # a narrowing revision keeps the original window
            # A watching client may first see this receipt up to SEEN_GAP_S after publication.
            delay = max(0, receipts[slot]["_ctime"] + bookings.SEEN_GAP_S - times[slot][0])
            if t < close + delay:
                open_windows.append(r["id"])
    reasons = [
        ("runs (including unknown occupancy)", [f"{e['id']} ({e['user']})" for e in entries if e["state"] != "ended"]),
        ("unaccounted run inboxes", unaccounted),
        ("pending or standing bookings", [r["id"] for r in rows
                                           if r["state"] == "pending" or r["state"] == "standing" and r["end"] > t]),
        ("open objection windows", open_windows),
        ("booking revisions without receipts", [r["id"] for r in rows if r["end"] > t and r["state"] == "unpublished"]),
        ("accounts without a move-capable build in the last 14 days", missing),
        ("source records could not be read completely", sorted(set(board.warnings))),
    ]
    blockers = [reason + ": " + ", ".join(records) for reason, records in reasons if records]
    if blockers:
        raise Refused("cutover requires an empty board\n" + "\n".join(blockers))
    return bookings.cutover_budget(board, rows, t)


def write_dirs(old):
    blocks = [("seq", name) for name in names_in(old, "seq") if name.isdecimal()]
    mailboxes = [("inbox", name) for name in names_in(old, "inbox") if old.inbox_owner(name)]
    return blocks + mailboxes + [tuple(path.split("/")) for path in setup.DIRS]


def directory_mode(old, parts, mode=None):
    parent = old.dir(*parts[:-1]) if len(parts) > 1 else old.fd
    fd = core.sub(parent, parts[-1]) if parent is not None else None
    if fd is None:
        return None
    try:
        st = os.fstat(fd)
        if st.st_uid != old.creator:
            return None
        if mode is not None:
            os.fchmod(fd, mode)
        return stat.S_IMODE(st.st_mode)
    finally:
        os.close(fd)


def prepare(old, path):
    """Keep a partial target private; the same command can finish an interrupted transaction."""
    pfd = core.walk(os.path.dirname(path))
    try:
        name = core.component(os.path.basename(path), "board directory")
        try:
            os.mkdir(name, 0o700, dir_fd=pfd)
        except FileExistsError:
            pass
        fd = core.sub(pfd, name)
        if fd is None:
            raise Refused(f"{path}: destination must be an empty directory")
        try:
            st = os.fstat(fd)
            if st.st_uid != old.creator or st.st_mode & (0o022 | stat.S_ISGID):
                raise Refused(f"{path}: destination must be owned and writable only by the source owner, without setgid")
            core.take_flock(fd, fcntl.LOCK_EX, core.BOARD_ADMIN_LEASE_S, path)
            marker = core.read_json(fd, core.MOVE_PENDING)
            pin = old.pin_fields()
            if marker is None:
                if os.listdir(fd):
                    raise Refused(f"{path}: destination must be empty")
                os.fchmod(fd, 0o700)
                core.publish(fd, core.MOVE_PENDING, core.dumps(pin))
            elif marker[0] != pin or marker[1].st_uid != old.creator or marker[1].st_mode & 0o022:
                raise Refused(f"{path}: destination belongs to another transaction")
            config = owner_file(old, "config")[0]
            blocks = sum(name.isdecimal() for name in names_in(old, "seq"))
            readme = owner_file(old, "README.md", core.MAX_RECORD)
            setup.populate(fd, config, blocks, None, [], readme[0] if readme else None)
            ino, fsid = core.identity(fd)
            return dict(path=path, inode=ino, fsid=fsid)
        finally:
            os.close(fd)
    finally:
        os.close(pfd)


def activate(old, new, source):
    require_owner(new)
    if (not matches(old, source) or old.creator != new.creator or new.host != core.HOST
            or owned_json(old, RECORD) != dict(new.pin_fields(), host=new.host)):
        raise Refused("the archived source does not authorize this destination")
    if new.pending:
        if owned_json(new, core.MOVE_PENDING) != source:
            raise Refused("the destination belongs to another transaction")
        os.fchmod(new.fd, 0o755)
        core.unlink_own(new.fd, core.MOVE_PENDING)


def cutover(old, new_path):
    require_owner(old)
    if old.host != core.HOST or old.pending:
        raise Refused("use an active source board on this host")
    pin = old.pin_fields()
    archive = retired(pin)
    if os.path.lexists(archive):
        raise Refused(f"archive path already exists: {archive}")
    modes = owned_json(old, SEAL, core.MOVE_LIMIT) or {}
    switched = False
    parent = core.walk(os.path.dirname(old.path))
    try:
        require_empty(old)
        if not modes:
            modes = {"/".join(parts): mode for parts in write_dirs(old)
                     if (mode := directory_mode(old, parts)) is not None}
            core.publish(old.fd, SEAL, core.dumps(modes))
        for part, mode in modes.items():
            directory_mode(old, part.split("/"), mode & ~0o222)
        budget = require_empty(old)
        target = prepare(old, new_path)
        with core.Board(new_path, (target["inode"], target["fsid"])).lease(exclusive=True) as new:
            require_owner(new)
            if not new.ok or new.settings != old.settings or owned_json(new, core.MOVE_PENDING) != pin:
                raise Refused("the prepared destination changed")
            origin = dict(source=pin, **budget)
            data = core.dumps(origin)
            if len(data) > core.MOVE_LIMIT:
                raise Refused("the carried booking budget exceeds the board limit")
            core.replace(new.fd, core.MOVE_ORIGIN, data)
            if owned_json(new, core.MOVE_ORIGIN, core.MOVE_LIMIT) != origin:
                raise Refused("could not verify the destination's budget record")
            if not old.recheck() or not new.recheck():
                raise Refused("a board path changed during the move")
            core.replace(old.fd, RECORD, core.dumps(dict(target, host=old.host)))
            os.rename(os.path.basename(old.path), os.path.basename(archive), src_dir_fd=parent, dst_dir_fd=parent)
            switched = True
            with core.Board(archive) as archived:
                activate(archived, new, pin)
    finally:
        try:
            if not switched:
                for part, mode in modes.items():
                    directory_mode(old, part.split("/"), mode)
                if modes:
                    core.unlink_own(old.fd, RECORD)
                    core.unlink_own(old.fd, SEAL)
        finally:
            os.close(parent)
    return archive


def command(old_path, new_path):
    if not confirmed():
        raise Refused("the installed upgrade is awaiting confirmation")
    old_path, new_path = os.path.abspath(old_path), os.path.abspath(new_path)
    if os.path.commonpath((old_path, new_path)) in (old_path, new_path):
        raise Refused("use separate source and destination paths")
    if any(c in old_path + new_path for c in "\r\n"):
        raise Refused("board paths must fit on one line")
    try:
        if os.path.lexists(old_path):
            with core.Board(old_path).lease(exclusive=True) as old:
                archive = cutover(old, new_path)
        else:
            with core.Board(new_path).lease(exclusive=True) as new:
                require_owner(new)
                pin = new.origin.get("source")
                if not pin or pin["path"] != old_path:
                    raise Refused("the destination has no move from this source")
                archive = retired(pin)
                with core.Board(archive) as old:
                    activate(old, new, pin)
    except (OSError, ValueError) as e:
        raise Refused(f"board move: {e}") from e
    print(f"source sealed at {archive}; board active at {new_path}", flush=True)
