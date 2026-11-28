"""Move a board without changing the authors or receipt order of its booking records."""
import hashlib
import os

from . import core
from .core import Refused

RECORD = "move.json"


def retired(pin):
    return f"{pin['path']}.retired-{int(pin['inode'])}"


def matches(board, pin):
    return board.ino == int(pin["inode"]) and board.fsid == int(pin["fsid"])


def owned_json(board, name):
    record = core.read_json(board.fd, name, 4096)
    if record and record[1].st_uid == board.creator and not record[1].st_mode & 0o022:
        return record[0]
    return None


def destination(old, pin):
    record = owned_json(old, RECORD)
    if not record or record.get("host") != core.HOST or old.host != core.HOST:
        return None
    path = record.get("path")
    if (not isinstance(path, str) or not os.path.isabs(path) or path != path.strip()
            or any(c in path for c in "\0\r\n")
            or path == pin["path"] or not all(core.is_int(record.get(k)) for k in ("inode", "fsid"))):
        return None
    try:
        new = core.Board(path, (record["inode"], record["fsid"]))
    except (OSError, ValueError):
        return None
    config = core.read_file(new.fd, "config", 4096)
    if (new.ok and new.creator == old.creator and new.host == core.HOST and new.limits == old.limits
            and not os.fstat(new.fd).st_mode & 0o022 and config and config[1].st_uid == new.creator
            and not config[1].st_mode & 0o022):
        return new
    new.close()
    return None


def follow(pin):
    """Follow a move only from the original pinned inode, now at its archive name."""
    if pin.get("host") != core.HOST:
        return None
    try:
        old = core.Board(retired(pin))
    except (OSError, ValueError):
        return None
    with old:
        if not matches(old, pin):
            return None
        new = destination(old, pin)
        if new is not None and new.pending:
            try:
                stamp = os.path.join(core.private_dir("board-moves"), hashlib.sha256(
                    core.dumps([old.pin_fields(), new.pin_fields()])).hexdigest() + ".json")
                if core.read_private(stamp) is None:
                    replay(old, new)
                    core.write_private(stamp, True)
            except BaseException:
                new.close()
                raise
        return new


def artifact_refs(board):
    for dirname, validator, stem in (
            ("bookings", lambda b: core.check_booking(b, board.ngpus), "id"),
            ("objections", core.check_objection, "id"),
            ("arrivals", core.check_arrival, "key")):
        for row in board.records(dirname, validator, stem_field=stem):
            yield (dirname,), row[stem] + ".json", row["user"], row["_sha"]
    for slot, row in board.receipts().items():
        yield ("seq", str(slot // 10000)), str(slot), row["user"], row["_sha"]


def replay(old, new):
    count = 0
    if not new.pending:
        raise Refused("the destination is not awaiting replay")
    for parts, name, user, sha in artifact_refs(old):
        if user != core.USER:
            continue
        fd = new.dir(*parts)
        if fd is None or os.fstat(fd).st_uid != new.creator:
            raise Refused(f"{'/'.join(parts)} must belong to the board owner")
        src = core.read_file(old.dir(*parts), name)
        if src is None or src[1].st_uid != core.UID or hashlib.sha256(src[0]).hexdigest() != sha:
            raise Refused(f"{'/'.join(parts)}/{name} changed during replay")
        try:
            core.publish(fd, name, src[0])
        except FileExistsError:
            cur = core.read_file(fd, name)
            if not cur or cur[1].st_uid != core.UID or cur[0] != src[0]:
                raise Refused(f"{'/'.join(parts)}/{name} is occupied on the destination")
        count += 1
    return count


def require_owner(board):
    config = core.read_file(board.fd, "config", 4096)
    if (board.creator != core.UID or os.fstat(board.fd).st_mode & 0o022
            or not config or config[1].st_uid != core.UID or config[1].st_mode & 0o022):
        raise Refused(f"{board.path}: run this as the board owner with an owner-writable root and config")


def stage(old, new):
    require_owner(old)
    require_owner(new)
    if old.path == new.path or old.host != core.HOST or new.host != core.HOST or old.limits != new.limits:
        raise Refused("use two boards on this host with the same GPU count and booking limits")
    if old.pending or new.entries() or any(artifact_refs(new)) or new.warnings:
        raise Refused("the source must be active and the destination must be empty")
    pin = old.pin_fields()
    archive = retired(pin)
    parent = core.walk(os.path.dirname(old.path))
    try:
        name = os.path.basename(old.path)
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (st.st_dev, st.st_ino) != (os.fstat(old.fd).st_dev, old.ino):
            raise Refused("the source changed while staging the move")
        if os.path.lexists(archive):
            raise Refused(f"archive path already exists: {archive}")
        pending = owned_json(new, core.MOVE_PENDING)
        if new.pending and pending != pin:
            raise Refused("the destination is already reserved for another move")
        core.replace(new.fd, core.MOVE_PENDING, core.dumps(pin))
        core.replace(old.fd, RECORD, core.dumps(dict(new.pin_fields(), host=new.host)))
        os.rename(name, os.path.basename(archive), src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        os.close(parent)
    print(f"source archived at {archive}; replay each author's booking records before finishing")


def command(action, old_path, new_path):
    old_path, new_path = os.path.abspath(old_path), os.path.abspath(new_path)
    try:
        with core.Board(new_path) as new:
            if action == "stage":
                with core.Board(old_path) as old:
                    stage(old, new)
                return
            pin = owned_json(new, core.MOVE_PENDING)
            if not pin or pin.get("path") != old_path:
                raise Refused("the destination has no pending move from this source")
            with core.Board(retired(pin)) as old:
                moved = destination(old, pin) if matches(old, pin) else None
                if moved is None:
                    raise Refused("the archived source does not authorize this move")
                with moved:
                    if moved.path != new.path or not matches(moved, new.pin_fields()):
                        raise Refused("the archived source names a different destination")
                    if action == "replay":
                        print(f"replayed {replay(old, new)} booking records owned by {core.USER}")
                        return
                    require_owner(new)
                    expected, actual = set(artifact_refs(old)), set(artifact_refs(new))
                    if old.warnings or new.warnings:
                        raise Refused("the source or destination calendar could not be read completely")
                    if expected != actual:
                        missing = ", ".join(sorted({a[2] for a in expected - actual})) or "none"
                        raise Refused(f"calendar differs: missing authors {missing}; "
                                      f"{len(actual - expected)} extra records")
                    present = {(e["id"], e["run"]) for e in new.entries() if new.occupancy(e) == "live"}
                    waiting = [e["id"] for e in old.entries()
                               if old.occupancy(e) == "live" and (e["id"], e["run"]) not in present]
                    if waiting:
                        raise Refused("waiting for fresh entries or old heartbeats to expire: " + ", ".join(waiting))
                    core.unlink_own(new.fd, core.MOVE_PENDING)
                    print(f"board active at {new.path} with {len(actual)} booking records")
    except (OSError, ValueError) as e:
        raise Refused(f"board move: {e}") from e
