"""Switch boards under a write lease. Replay creates files only as their original author."""
import fcntl
import hashlib
import os
import stat
import time

from . import core
from .core import Refused

RECORD = "move.json"
SEAL = ".move-seal"
CLIENT_PREFIX = "account-build-"
MOVE_PROTOCOL = 1
PUBLISHER_LOOKBACK_S = 14 * 86400
CUTOVER_MARGIN_S = 300
RECEIPT_DRAIN_S = 3


def confirmed():
    from . import auto_upgrade
    return not auto_upgrade.read_state().get("pending")


def announce(board, build):
    if not board.ok or board.pending or not confirmed():
        return
    try:
        data = core.dumps(dict(user=core.USER, build=build, move_protocol=MOVE_PROTOCOL))
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


def destination(old, pin):
    record = owned_json(old, RECORD)
    if not record or record.get("host") != core.HOST or old.host != core.HOST:
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
    if (new.ok and new.creator == old.creator and new.host == core.HOST and new.limits == old.limits
            and not os.fstat(new.fd).st_mode & 0o022 and owner_file(new, "config")
            and new.origin.get("source") == dict(path=pin["path"], inode=old.ino, fsid=old.fsid)
            and not new.pending):
        return new
    new.close()
    return None


def follow(pin):
    """The archive must be the exact pinned source, even if someone reuses its old name."""
    if pin.get("host") != core.HOST or not confirmed():
        return None
    try:
        old = core.Board(retired(pin))
    except (OSError, ValueError):
        return None
    with old:
        if not matches(old, pin):
            return None
        new = destination(old, pin)
        if new is not None:
            try:
                replay(old, new)
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


def copy_owned(old, new, parts, name, sha, target=None):
    target_parts, target_name = target or (parts, name)
    fd = new.dir(*target_parts)
    if fd is None or os.fstat(fd).st_uid != new.creator:
        raise Refused(f"{'/'.join(parts)} must belong to the board owner")
    src = core.read_file(old.dir(*parts), name)
    if src is None or src[1].st_uid != core.UID or hashlib.sha256(src[0]).hexdigest() != sha:
        raise Refused(f"{'/'.join(parts)}/{name} changed during migration")
    try:
        core.publish(fd, target_name, src[0])
    except FileExistsError:
        cur = core.read_file(fd, target_name)
        if not cur or cur[1].st_uid != core.UID or cur[0] != src[0]:
            raise Refused(f"{'/'.join(parts)}/{name} is occupied on the destination")


def record_end(board, entry):
    record = dict(id=entry["id"], run=entry["run"], state="ended")
    fd = board.wdir("migration")
    name = f"{entry['id']}.ended.json"
    try:
        core.publish(fd, name, core.dumps(record))
    except FileExistsError:
        pass


def publish_entry(old, new, entry):
    state = core.read_private(os.path.join(core.STATE(), "runs", f"{entry['id']}.json")) or {}
    if state.get("state") == "ended" and state.get("run") == entry["run"]:
        record_end(new, entry)
        return
    rid = entry["id"]
    inbox = core.mkdir_own(new.wdir("inbox"), rid, new.inbox_mode)
    if inbox is not None:
        os.close(inbox)
    copy_owned(old, new, ("agents",), f"{rid}.json", entry["_sha"])
    old_hb = old.dir("agents", ".heartbeat")
    hb = new.wdir("agents", ".heartbeat")
    if old_hb is None:
        return
    original = core.read_file(old_hb, rid, 4096)
    if original is None or original[1].st_uid != core.UID or original[1].st_mode & 0o022:
        return
    data, st = original
    try:
        core.publish(hb, rid, data)
    except FileExistsError:
        return
    os.utime(rid, ns=(st.st_atime_ns, st.st_mtime_ns), dir_fd=hb, follow_symlinks=False)


def replay(old, new):
    # This lock is private to the author. Its shared completion marker is published after the last record.
    lock = os.path.join(core.private_dir("board-moves"), f"{new.fsid}-{new.ino}.lock")
    with open(lock, "a") as f:
        core.take_flock(f, fcntl.LOCK_EX, core.BOARD_LEASE_S, "author replay")
        marker = f"{core.USER}.json"
        fd = new.wdir("migration")
        done = core.read_file(fd, marker)
        if done and done[1].st_uid == core.UID and not done[1].st_mode & 0o022:
            return
        for parts, name, user, sha in artifact_refs(old):
            if user == core.USER:
                copy_owned(old, new, parts, name, sha)
        old_inboxes = old.dir("inbox")
        names, _ = core.scan(old_inboxes) if old_inboxes is not None else ([], 0)
        for name in names or []:
            if core.owned_id(name, core.USER) and old.inbox_owner(name) == core.USER:
                inbox = core.mkdir_own(new.wdir("inbox"), name, new.inbox_mode)
                if inbox is not None:
                    os.close(inbox)
            if old.inbox_owner(name) is None:
                continue
            prefix = core.move_mail_prefix(name)
            messages = old.records(f"inbox/{name}", core.check_message, owner_field="sender")
            for m in messages:
                if (m["sender"] == core.USER and m["to"] == name and m["_mode"] == 0o644
                        and m["_mtime"] >= new.origin["staged_at"]):
                    filename = m["id"] + ".json"
                    copy_owned(old, new, ("inbox", name), filename, m["_sha"],
                               target=(("migration",), prefix + filename))
        for e in old.entries():
            if e["user"] == core.USER:
                publish_entry(old, new, e)
        core.publish(fd, marker, b"{}\n")


def completed(board):
    fd = board.dir("migration")
    names, _ = core.scan(fd) if fd is not None else ([], 0)
    out = {}
    for name in names or []:
        if name.endswith(".json") and not name.endswith(".ended.json"):
            row = core.read_file(fd, name, 4096)
            if row and core.owner_name(row[1].st_uid) == name[:-5] and not row[1].st_mode & 0o022:
                out[name[:-5]] = int(row[1].st_ctime)
    return out


def legacy_runs(board, entries):
    """An old entry is evidence to hold a GPU until its author takes over or reports its verified end."""
    holds = getattr(board, "origin", {}).get("runs", [])
    if not holds:
        return []
    seen = {(e["id"], e["run"]) for e in entries}
    fd = board.dir("migration")
    for e in holds:
        r = core.read_json(fd, f"{e['id']}.ended.json") if fd is not None else None
        if (r and core.owner_name(r[1].st_uid) == e["user"] and not r[1].st_mode & 0o022
                and r[0].get("id") == e["id"] and r[0].get("run") == e["run"] and r[0].get("state") == "ended"):
            seen.add((e["id"], e["run"]))
    return [e for e in holds if (e["id"], e["run"]) not in seen]


def leftovers(board, entries=None):
    ready = completed(board)
    runs = legacy_runs(board, board.entries() if entries is None else entries)
    bookings = []
    for b in board.origin.get("bookings", []):
        if b["end"] <= core.now():
            continue
        missing = [u for u in [b["user"], *b["objectors"]] if u not in ready]
        if missing:
            bookings.append(dict(b, awaiting=missing))
    return runs, bookings


def manifest(old, t):
    from . import bookings

    entries = bookings.entries_with_occupancy(old)
    receipts = old.receipts()
    times = bookings.sequence_times(receipts, None, getattr(old, "origin", {}))
    rows = bookings.evaluate(old, t, entries=entries, receipts=receipts, record=False)
    objections = {}
    received = {(r["kind"], r["id"], r["revision"]): times[slot][0]
                for slot, r in receipts.items() if not times[slot][1]}
    for o in old.objections():
        when = received.get(("objection", o["id"], o["_sha"]))
        if when is not None:
            objections.setdefault((o["booking"], o["booking_revision"]), []).append((o["user"], when))
    future = []
    for row in rows:
        if row["end"] <= t or row.get("order") is None:
            continue
        guard = {k: row[k] for k in ("id", "user", "gpus", "start", "end", "revision", "order", "objection_close",
                                    "state")}
        revisions = [row["revision"], *row.get("lineage", [])]
        guard["objectors"] = sorted({u for rev in revisions for u, when in objections.get((row["id"], rev), [])
                                    if u != row["user"] and when < row["objection_close"]})
        future.append(guard)
    runs = [{k: e[k] for k in ("id", "user", "gpus", "run")} for e in entries]
    runs.extend(legacy_runs(old, entries))
    if old.warnings:
        raise Refused("the source could not be read completely")
    clocks = dict(old.origin.get("times", {}))
    clocks.update({str(s): [when, receipts[s]["_sha"]] for s, (when, replaced) in times.items() if not replaced})
    return dict(at=t, floor=max(old.receipt_floor, max(receipts, default=0)), source=old.pin_fields(),
                times=clocks, runs=runs, bookings=future)


def require_owner(board):
    if board.creator != core.UID or os.fstat(board.fd).st_mode & 0o022 or not owner_file(board, "config"):
        raise Refused(f"{board.path}: run this as the board owner with an owner-writable root and config")


def capable(row):
    return row.get("move_protocol") == MOVE_PROTOCOL and bool(row.get("build")) and core.note_ok(row["build"], 128)


def names_in(board, *parts):
    fd = board.dir(*parts)
    names, _ = core.scan(fd) if fd is not None else ([], 0)
    if names is None:
        raise Refused(f"cannot enumerate {'/'.join(parts)} for cutover")
    return names


def publisher_readiness(board, entries, receipts, t):
    authors, versions, old_runs = set(), {}, []

    def observe(user, when, build=None, is_capable=False):
        if when >= t - PUBLISHER_LOOKBACK_S:
            authors.add(user)
        if build is not None and when >= versions.get(user, (0, "", False))[0]:
            versions[user] = when, build, is_capable

    heartbeat = board.dir("agents", ".heartbeat")
    for e in entries:
        hb = core.read_file(heartbeat, e["id"], 4096) if heartbeat is not None else None
        matching = not hb or (
            core.owner_name(hb[1].st_uid) == e["user"] and not hb[1].st_mode & 0o022
            and hb[0].decode("utf-8", "replace").strip() == e.get("build"))
        observe(e["user"], max(e["_ctime"], hb[1].st_ctime if hb and matching else 0),
                e.get("build", "unknown"), capable(e) and matching)
        if not capable(e) or not matching:
            old_runs.append(f"{e['id']} ({e['user']}, {e.get('build', 'unknown')})")

    for name in names_in(board, "archive"):
        if not name.endswith(".json"):
            continue
        row = core.read_json(board.dir("archive"), name)
        if row:
            e, st, _ = row
            user = core.owner_name(st.st_uid)
            if core.check_entry(e, board.ngpus) and e["user"] == user and not st.st_mode & 0o022:
                observe(user, st.st_ctime, e.get("build", "unknown"), capable(e))

    for e in [*board.bookings_raw(), *board.objections(), *board.arrivals(), *receipts.values()]:
        observe(e["user"], e["_ctime"])
    for name in names_in(board, "log"):
        st = os.stat(name, dir_fd=board.dir("log"), follow_symlinks=False)
        if stat.S_ISREG(st.st_mode) and name == core.owner_name(st.st_uid) + ".jsonl":
            observe(core.owner_name(st.st_uid), st.st_ctime)
    for box in names_in(board, "inbox"):
        if board.inbox_owner(box) is not None:
            for m in board.messages(box):
                observe(m["sender"], m["_ctime"])

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
    missing = [f"{user} ({versions.get(user, (0, 'unknown', False))[1]})" for user in sorted(authors)
               if not versions.get(user, (0, "", False))[2]]
    return missing, sorted(old_runs)


def quiet(board, *, drain_receipts=False):
    from . import bookings

    deadline = time.monotonic() + (RECEIPT_DRAIN_S if drain_receipts else 0)
    while True:
        t = core.now()
        entries = bookings.entries_with_occupancy(board)
        receipts = board.receipts()
        rows = bookings.evaluate(board, t, entries=entries, receipts=receipts, record=False)
        incomplete = [r["id"] for r in rows if r["end"] > t and r["state"] == "unpublished"]
        if not incomplete or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if incomplete:
        raise Refused("booking revision has no completed receipt: " + ", ".join(incomplete))
    missing, old_runs = publisher_readiness(board, entries, receipts, t)
    open_windows = [r["id"] for r in rows if r["state"] == "pending" or t < (r.get("objection_close") or 0)]
    overlaps = [r["id"] for r in rows if r["user"] != core.USER and r["state"] == "standing"
                and r["end"] > t and max(r["start"], r["objection_close"]) <= t + CUTOVER_MARGIN_S]
    reasons = []
    for reason, records in (("accounts without a move-capable build", missing),
                            ("runs without a move-capable build", old_runs),
                            ("open objection windows", open_windows),
                            ("other accounts' bookings within five minutes", overlaps)):
        if records:
            reasons.append(reason + ": " + ", ".join(records))
    if board.warnings:
        reasons.append("source records could not be read completely")
    if reasons:
        raise Refused("cutover requires a quiet board\n" + "\n".join(reasons))


def unreplayed_standing(board, t):
    old = getattr(board, "origin", {}).get("bookings", [])
    if not old:
        return []
    ready = completed(board)
    return [b for b in old if b["user"] not in ready and b["state"] == "standing"
            and max(b["start"], b["objection_close"]) <= t < b["end"]]


def stage(old, new):
    require_owner(old)
    require_owner(new)
    if old.host != core.HOST or new.host != core.HOST or old.limits != new.limits:
        raise Refused("use two boards on this host with the same GPU count and booking limits")
    if old.pending or new.origin or new.entries() or any(artifact_refs(new)) or new.warnings:
        raise Refused("the source must be active and the destination empty")
    quiet(old)
    if new.pending:
        if owned_json(new, core.MOVE_PENDING) != old.pin_fields():
            raise Refused("the destination is reserved for another move")
    else:
        core.replace(new.fd, core.MOVE_PENDING, core.dumps(old.pin_fields()))
    print(f"destination prepared at {new.path}; source remains active at {old.path}")


def write_dirs(old):
    blocks = [("seq", name) for name in names_in(old, "seq") if name.isdecimal()]
    mailboxes = [("inbox", name) for name in names_in(old, "inbox") if old.inbox_owner(name)]
    return blocks + mailboxes + [
        ("bookings",), ("objections",), ("arrivals",), ("agents",), ("agents", ".heartbeat"),
        ("inbox",), ("archive",), ("log",), ("seq",), ("migration",)]


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


def finish(old, new, fenced_at):
    require_owner(old)
    require_owner(new)
    pin = old.pin_fields()
    archive = retired(pin)
    if owned_json(new, core.MOVE_PENDING) != pin or os.path.lexists(archive):
        raise Refused("the destination is not staged for this source or the archive name is taken")
    quiet(old, drain_receipts=True)
    parent = core.walk(os.path.dirname(old.path))
    modes = {}
    switched = False
    try:
        name = os.path.basename(old.path)
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (st.st_dev, st.st_ino) != (os.fstat(old.fd).st_dev, old.ino):
            raise Refused("the source changed while finishing the move")
        saved = owned_json(old, SEAL, core.MOVE_LIMIT)
        if saved is None:
            modes = {"/".join(parts): mode for parts in write_dirs(old)
                     if (mode := directory_mode(old, parts)) is not None}
            core.replace(old.fd, SEAL, core.dumps(modes))
        else:
            modes = saved
        for part, mode in modes.items():
            directory_mode(old, part.split("/"), mode & ~0o222)
        quiet(old)
        origin = manifest(old, fenced_at)
        origin["staged_at"] = owner_file(new, core.MOVE_PENDING)[1].st_mtime
        if new.dir("seq", str((origin["floor"] + 1) // 10000)) is None:
            raise Refused("allocate more receipt blocks on the destination before finishing")
        data = core.dumps(origin)
        if len(data) > core.MOVE_LIMIT:
            raise Refused("the source migration index exceeds the board limit")
        core.replace(new.fd, core.MOVE_ORIGIN, data)
        core.replace(old.fd, RECORD, core.dumps(dict(new.pin_fields(), host=new.host)))
        os.rename(name, os.path.basename(archive), src_dir_fd=parent, dst_dir_fd=parent)
        switched = True
        core.unlink_own(new.fd, core.MOVE_PENDING)
    finally:
        try:
            if not switched:
                for part, mode in modes.items():
                    directory_mode(old, part.split("/"), mode)
                core.unlink_own(old.fd, SEAL)
        finally:
            os.close(parent)
    print(f"source sealed at {archive}; board active at {new.path}", flush=True)


def command(action, old_path, new_path, wait):
    old_path, new_path = os.path.abspath(old_path), os.path.abspath(new_path)
    if old_path == new_path or not 0 <= wait <= 30:
        raise Refused("use distinct source and destination paths and a wait of 0–30 seconds")
    try:
        if action == "finish" and not os.path.lexists(old_path):
            with core.Board(new_path).lease(exclusive=True) as new:
                require_owner(new)
                pin = new.origin.get("source")
                if not pin or pin["path"] != old_path:
                    raise Refused("the destination has no move from this source")
                with core.Board(retired(pin)) as old:
                    record = owned_json(old, RECORD)
                    if not matches(old, pin) or record != dict(new.pin_fields(), host=new.host):
                        raise Refused("the archived source does not authorize this destination")
                    core.unlink_own(new.fd, core.MOVE_PENDING)
        else:
            with core.Board(old_path).lease(exclusive=action == "finish") as old:
                fenced_at = core.now()
                with core.Board(new_path).lease(exclusive=True) as new:
                    if action == "stage":
                        return stage(old, new)
                    finish(old, new, fenced_at)
        deadline = time.monotonic() + wait
        while True:
            with core.Board(new_path).lease() as new:
                runs, bookings = leftovers(new)
            if not (runs or bookings) or time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        for record in runs:
            print(f"old run {record['id']} remains unconfirmed; exclusive handoff still holds its GPUs")
        for record in bookings:
            print(f"old booking {record['id']} awaits replay by {', '.join(record['awaiting'])}")
    except (OSError, ValueError) as e:
        raise Refused(f"board move: {e}") from e
