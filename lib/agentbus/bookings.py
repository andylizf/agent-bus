"""Bookings as public notices: receipts in an append-only sequence, the objection window, limits and booking states.

A booking revision is published when a receipt naming its sha256 is claimed in seq/<k>/<n> by link (the filesystem's
EEXIST race assigns the slot, so no writer picks its place). States, computed in this order (design 4.2):
unpublished, invalid / over-budget, conflicting, objected, pending, standing (and lapsed per the no-show rule).
"""
import fcntl, hashlib, json, os, secrets, time

from . import core, move
from .core import Refused, dumps, now

SEEN_GAP_S = 30          # a reader that read the sequence this recently is watching: new slots are arrivals it saw
SEEN_KEEP_S = 9 * 86400  # slots-seen records older than this are dropped (every booking they could order has ended)
CHAIN_MAX = 4            # previous revisions a narrowing revision may carry


# ------------------------------------------------------------------------------------------------ receipts

def claim_receipt(board, kind, rid, revision):
    """Claim the next free slot for a receipt; returns the slot."""
    board.writable()
    recs = board.receipts()
    n = max(max(recs, default=0), board.receipt_floor) + 1
    for _ in range(10000):
        k = n // 10000
        bfd = board.dir("seq", str(k))
        if bfd is None or os.fstat(bfd).st_uid != board.creator:
            raise Refused(f"receipt block seq/{k} is missing or not the board creator's: the board needs more blocks "
                          f"(setup-board.sh --add-blocks)")
        body = dict(slot=n, kind=kind, id=rid, revision=revision, user=core.USER, created=now())
        try:
            core.publish(bfd, str(n), dumps(body))
            return n
        except FileExistsError:
            n += 1
    raise Refused("could not claim a receipt slot")


def seen_update(receipts, imported_through=0):
    """Record each receipt slot this user's bus processes have read (~/.local/state/agent-bus/slots.json, under
    lockf on slots.lock): {slot: {sha, first_seen, rec}}. first_seen is an observed arrival only when the previous
    read was at most SEEN_GAP_S ago; otherwise it is None and the sequence time t(n) alone applies."""
    d = core.private_dir()
    lock = open(os.path.join(d, "slots.lock"), "a+")
    try:
        fcntl.lockf(lock, fcntl.LOCK_EX)
        path = os.path.join(d, "slots.json")
        data = core.read_private(path) or {}
        slots = data.get("slots") or {}
        t = time.time()
        last = data.get("last_read")
        watching = isinstance(last, (int, float)) and t - last <= SEEN_GAP_S
        for slot, r in receipts.items():
            s = str(slot)
            if s not in slots:
                slots[s] = dict(sha=r["_sha"], first_seen=t if watching and slot > imported_through else None, rec=t)
        for s in [s for s, v in slots.items() if t - (v.get("rec") or 0) > SEEN_KEEP_S]:
            del slots[s]
        core.write_private(path, dict(slots=slots, last_read=t))
        return slots
    finally:
        lock.close()


def sequence_times(receipts, seen, origin=None):
    """{slot: (reader receipt time, replaced)}: t(n) = max(created(n), t(n-1)) along the sequence; a reader that saw
    the slot arrive also takes the later of t(n) and its first sight; a slot whose receipt differs from what this
    reader recorded for it is replaced."""
    origin = origin or {}
    imported = origin.get("times", {})
    floor = origin.get("floor", 0)
    floor_time = max((r[0] for r in imported.values()), default=0)
    out, prev = {}, None
    for slot in sorted(receipts):
        r = receipts[slot]
        tn = r["created"] if prev is None else max(r["created"], prev)
        original = imported.get(str(slot))
        if original is not None:
            tn = original[0]
        elif slot > floor:
            tn = max(tn, floor_time)
        prev = tn
        rec = (seen or {}).get(str(slot))
        replaced = (bool(rec) and rec.get("sha") != r["_sha"]) or (
            slot <= floor and (original is None or original[1] != r["_sha"]))
        tr = max(tn, rec["first_seen"]) if rec and rec.get("first_seen") else tn
        out[slot] = (tr, replaced)
    return out


# ------------------------------------------------------------------------------------------------ states

def overlap(a, b):
    return a["start"] < b["end"] and b["start"] < a["end"] and bool(set(a["gpus"]) & set(b["gpus"]))


def narrows(new, old):
    return set(new["gpus"]) <= set(old["gpus"]) and new["start"] >= old["start"] and new["end"] <= old["end"]


def gpu_hours_in(b, s0, s1):
    return max(0, min(b["end"], s1) - max(b["start"], s0)) * len(b["gpus"]) / 3600.0


def over_budget(mine, b, lim):
    """True when adding b to the owner's counted bookings `mine` puts some 7-day span over the budget."""
    span = lim["budget_span_s"]
    allb = mine + [b]
    starts = {x["start"] for x in allb} | {x["end"] - span for x in allb}
    for s in starts:
        if sum(gpu_hours_in(x, s, s + span) for x in allb) > lim["budget_gpu_h"] + 1e-9:
            return True
    return False


def evaluate(board, t=None, seen=None, entries=None, sticky_lapsed=(), receipts=None, record=True, cache=None):
    """Every booking file on the board with its state. Returns a list of dicts: the booking's fields plus slot,
    order, revision, receipt_time, objection_close, state, why, objections."""
    t = now() if t is None else t
    lim = board.limits
    origin = getattr(board, "origin", {})
    inherited = {b["id"]: b for b in origin.get("bookings", [])}
    ready = move.completed(board) if inherited else {}
    recs = receipts if receipts is not None else board.receipts(cache)
    if seen is None and record:
        seen = seen_update(recs, origin.get("floor", 0))
    times = sequence_times(recs, seen, origin)
    by_rev = {}
    for slot in sorted(recs):
        r = recs[slot]
        by_rev.setdefault((r["kind"], r["id"], r["revision"]), []).append(slot)

    def receipt_of(kind, rid, rev):
        """Lowest slot for this revision whose receipt is not replaced, and whether a replaced one was met."""
        for slot in by_rev.get((kind, rid, rev), []):
            if not times[slot][1]:
                return slot, False
            return slot, True
        return None, False

    objs = {}
    for o in board.objections():
        slot, repl = receipt_of("objection", o["id"], o["_sha"])
        if slot is None or repl:
            continue
        objs.setdefault((o["booking"], o["booking_revision"]), []).append(dict(o, _slot=slot))

    def objections_for(b, rev, close):
        got, users = [], set()
        for o in sorted(objs.get((b["id"], rev), []), key=lambda o: o["_slot"]):
            if o["user"] == b["user"] or o["user"] in users:
                continue
            if times[o["_slot"]][0] < close:
                users.add(o["user"])
                got.append(dict(id=o["id"], user=o["user"], reason=o["reason"], slot=o["_slot"]))
        return got

    revoked = set()   # bookings whose predecessor turned out not to have been standing: no inheritance

    def base(b, rev, depth=0):
        """Receipt facts of one revision: slot, receipt time, close, order slot, objections, inherited."""
        slot, repl = receipt_of("booking", b["id"], rev)
        if slot is None:
            return None
        rt = times[slot][0]
        info = dict(slot=slot, order=slot, receipt_time=rt, objection_close=rt + b["objection_s"], replaced=repl,
                    inherited=False)
        prev = b.get("previous")
        if isinstance(prev, str) and depth < CHAIN_MAX and not (depth == 0 and b["id"] in revoked):
            try:
                pb = json.loads(prev)
            except ValueError:
                pb = None
            if (isinstance(pb, dict) and core.check_booking(pb, board.ngpus) and pb["id"] == b["id"]
                    and pb["user"] == b["user"]):
                prev_rev = hashlib.sha256(prev.encode()).hexdigest()
                pi = base(pb, prev_rev, depth + 1)
                if (pi and pi["slot"] < slot and not pi["replaced"] and pi["objection_close"] <= rt
                        and not objections_for(pb, prev_rev, pi["objection_close"]) and narrows(b, pb)):
                    info.update(order=pi["order"], objection_close=pi["objection_close"], inherited=True,
                                inherits=prev_rev, pred=pb, pred_info=pi,
                                lineage=[prev_rev] + pi.get("lineage", []))
        return info

    def invalid_why(b, receipt_time):
        if b["end"] <= b["start"]:
            return "ends before it starts"
        if b["end"] - b["start"] > lim["max_len_s"]:
            return f"longer than {lim['max_len_s'] // 3600} h"
        if b["start"] > receipt_time + lim["horizon_s"]:
            return f"starts more than {lim['horizon_s'] // 86400} days after its receipt"
        if b["objection_s"] < lim["min_objection_s"]:
            return f"objection window under {lim['min_objection_s'] // 60} min"
        return None

    def compute():
        rows = []
        for b in board.bookings_raw():
            rev = b["_sha"]
            info = base(b, rev)
            row = dict(core.clean(b), revision=rev)
            row.pop("previous", None)
            if info is None:
                row.update(state="unpublished", why="no receipt names this revision", slot=None, order=None)
                rows.append(row)
                continue
            row.update(slot=info["slot"], order=info["order"], receipt_time=info["receipt_time"],
                       objection_close=info["objection_close"], inherited=info["inherited"],
                       _pred=info.get("pred"), _pred_info=info.get("pred_info"),
                       lineage=info.get("lineage", []) if info["inherited"] else [])
            if info["replaced"]:
                row.update(state="invalid", why=f"receipt slot {info['slot']} was replaced since this reader saw it")
            elif invalid_why(b, info["receipt_time"]):
                row.update(state="invalid", why=invalid_why(b, info["receipt_time"]))
            rows.append(row)

        # receipt order: conflicts and the budget are decided earliest first
        live = sorted([r for r in rows if "state" not in r], key=lambda r: (r["order"], r["slot"]))
        counted = {}      # user -> bookings counted against the budget
        blockers = []     # earlier bookings that make later overlapping ones conflicting
        for r in live:
            r["objections"] = [] if r.get("inherited") else objections_for(r, r["revision"], r["objection_close"])
            if r.get("inherited"):  # inherited objections never count: the narrowed revision was standing
                r["objections"] = []
            conflict = next((o for o in blockers if o["user"] != r["user"] and overlap(o, r)), None)
            if conflict:
                r.update(state="conflicting", why=f"overlaps {conflict['id']} ({conflict['user']}), received earlier")
                blockers.append(r)
                continue
            if r["objections"]:
                o = r["objections"][0]
                r.update(state="objected", why=f"objection by {o['user']}: {o['reason']}")
                continue
            mine = counted.setdefault(r["user"], [])
            if over_budget(mine, r, lim):
                r.update(state="over-budget", why=f"over {lim['budget_gpu_h']:g} GPU-h in a 7-day span")
                continue
            mine.append(r)
            blockers.append(r)
            if t < r["objection_close"]:
                r.update(state="pending", why="objection window open")
            else:
                r.update(state="standing", why="")
        return rows

    def predecessor_standing(r, rows):
        """A narrowing revision inherits only from a predecessor that was standing in full: valid under the limits,
        not objected (checked in base), not conflicting with another user's earlier booking, within the budget."""
        links, pb, pi = [], r["_pred"], r["_pred_info"]
        while pb is not None:              # every ancestor in the chain, each with its own receipt slot
            links.append((pb, pi))
            pb, pi = (pi.get("pred"), pi.get("pred_info")) if pi.get("inherited") else (None, None)
        mine = [o for o in rows if o["user"] == r["user"] and o["id"] != r["id"]
                and o.get("state") in ("standing", "pending", "lapsed")]
        for pb, pi in links:
            if invalid_why(pb, pi["receipt_time"]) or over_budget(mine, pb, lim):
                return False
            for o in rows:
                if (o["id"] != r["id"] and o["user"] != r["user"] and o.get("order") is not None
                        and o["order"] < pi["slot"] and o.get("state") in ("standing", "pending", "lapsed", "conflicting")
                        and overlap(o, pb)):
                    return False
        return True

    for _ in range(CHAIN_MAX + 2):
        rows = compute()
        bad = [r["id"] for r in rows if r.get("inherited") and not predecessor_standing(r, rows)]
        if not bad:
            break
        revoked.update(bad)
    arrived = {}
    for x in (board.arrivals() if hasattr(board, "arrivals") else []):
        arrived.setdefault(x["id"], []).append(x)
    for r in rows:
        r.setdefault("objections", [])
        if r.get("state") == "standing":
            before = inherited.get(r["id"])
            revisions = [r["revision"], *r.get("lineage", [])]
            if before and before["revision"] in revisions and any(u not in ready for u in before["objectors"]):
                r.update(state="pending", why="waiting for an objection from the previous board")
            elif any(b["user"] != r["user"] and b["user"] not in ready and b["end"] > t and b["order"] < r["order"]
                     and b["state"] in ("pending", "standing", "conflicting", "lapsed") and overlap(b, r)
                     for b in inherited.values()):
                r.update(state="pending", why="waiting for an earlier overlapping booking from the previous board")
        if r.get("state") == "standing":
            lapse_at = max(r["start"], r["objection_close"]) + lim["lapse_s"]
            revs = [r["revision"]] + (r.get("lineage") or [])
            if any(f"{r['id']}@{v}" in sticky_lapsed for v in revs):     # remembered for this revision only
                r.update(state="lapsed", why="no run of its owner on it within the no-show limit")
            elif t >= lapse_at and entries is not None and not any(
                    e.get("booking") == r["id"] and e["user"] == r["user"] and e["state"] in ("running", "waiting-handoff")
                    and e.get("booking_revision") in revs and set(e.get("gpus") or []) <= set(r["gpus"])
                    and e.get("occupancy") == "live" and e["started"] <= lapse_at for e in entries) and not any(
                    x["user"] == r["user"] and x["revision"] in [r["revision"]] + (r.get("lineage") or [])
                    and max(r["start"], r["objection_close"]) - lim["lapse_s"] <= x["arrived"] <= lapse_at
                    for x in arrived.get(r["id"], [])):
                r.update(state="lapsed", why=f"no run of its owner on it by {time.strftime('%H:%M', time.localtime(lapse_at))}")
            r["lapse_at"] = lapse_at
        r["active"] = r.get("state") == "standing" and max(r["start"], r["objection_close"]) <= t < r["end"]
    for r in rows:
        r.pop("_pred", None)
        r.pop("_pred_info", None)
    return sorted(rows, key=lambda r: (r.get("order") or 1 << 60, r["id"]))


def entries_with_occupancy(board):
    out = []
    for e in board.entries():
        e = dict(e)
        age = board.heartbeat_age(e)
        e["heartbeat_age"] = None if age is None else int(age)
        e["occupancy"] = "live" if age is not None and age < core.HB_STALE_S() else "unknown"
        out.append(e)
    return out


def standing_active_on(rows, gpus, t, exclude=None):
    """Standing, active, unlapsed bookings on any of `gpus` at time t, other than `exclude`."""
    return [r for r in rows if r.get("state") == "standing" and r["id"] != exclude and set(r["gpus"]) & set(gpus)
            and max(r["start"], r["objection_close"]) <= t < r["end"]]


# ------------------------------------------------------------------------------------------------ publishing

def parse_when(text):
    """Round 1 booking times: ISO 8601 with an offset, or epoch seconds."""
    from datetime import datetime
    s = str(text).strip()
    if s.isdigit():
        return int(s)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise Refused(f"cannot read time {text!r}: give ISO 8601 with an offset (2026-11-24T22:00-05:00) or epoch "
                      f"seconds") from None
    if d.tzinfo is None:
        raise Refused(f"{text!r} has no UTC offset; round 1 takes ISO times with an offset or epoch seconds")
    return int(d.timestamp())


def parse_duration(text):
    s = str(text).strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}
    try:
        return int(float(s[:-1]) * mult[s[-1]]) if s and s[-1] in mult else int(s)
    except ValueError:
        raise Refused(f"cannot read duration {text!r}: like 30m, 600s or 1h") from None


def publish_booking(board, gpus, start, end, objection_s=None, kind="other", note="", source="cli", ref=None,
                    slug=None, revise=None):
    """Publish a booking (or a revision of one's own booking) and claim its receipt. Refuses what the limits make
    invalid or over budget."""
    board.writable()
    lim = board.limits
    objection_s = lim["default_objection_s"] if objection_s is None else objection_s
    t = now()
    if kind not in core.KINDS:
        raise Refused(f"kind must be one of {', '.join(core.KINDS)}")
    if not core.note_ok(note):
        raise Refused(f"note: at most {core.NOTE_MAX} characters, no control characters")
    if end <= start:
        raise Refused("the booking ends before it starts")
    if end - start > lim["max_len_s"]:
        raise Refused(f"a booking covers at most {lim['max_len_s'] // 3600} h")
    if start > t + lim["horizon_s"]:
        raise Refused(f"a booking starts at most {lim['horizon_s'] // 86400} days ahead")
    if objection_s < lim["min_objection_s"]:
        raise Refused(f"the objection window is at least {lim['min_objection_s'] // 60} min")
    if end <= t:
        raise Refused("the booking ends in the past")
    fd = board.wdir("bookings")
    previous = None
    if revise:
        core.component(revise, "booking id")
        r = core.read_file(fd, f"{revise}.json")
        if r is None or r[1].st_uid != core.UID:
            raise Refused(f"no booking {revise} of yours")
        prev = json.loads(r[0])
        previous, bid, created = r[0].decode(), revise, prev.get("created", t)
        # a chain longer than CHAIN_MAX is cut: the oldest revisions drop out of it
        depth, p = 0, prev
        while isinstance(p.get("previous"), str) and depth < CHAIN_MAX:
            depth += 1
            p = json.loads(p["previous"])
        if depth >= CHAIN_MAX - 1:
            previous = None
    else:
        bid, created = core.new_id(slug or note or "book", t), t
    b = dict(id=bid, user=core.USER, gpus=sorted(gpus), start=int(start), end=int(end), created=created,
             objection_s=int(objection_s), kind=kind, note=note, source=source, ref=ref, schema=core.SCHEMA)
    if previous:
        b["previous"] = previous
    entries = entries_with_occupancy(board)
    rows = evaluate(board, t, entries=entries)
    mine = [r for r in rows if r["user"] == core.USER and r["id"] != bid
            and r.get("state") in ("pending", "standing", "lapsed")]
    if over_budget(mine, b, lim):
        raise Refused(f"over your budget: {lim['budget_gpu_h']:g} GPU-h of bookings in any 7-day span")
    data = dumps(b)
    if revise:
        core.replace(fd, f"{bid}.json", data)
    else:
        for _ in range(5):
            try:
                core.publish(fd, f"{bid}.json", data)
                break
            except FileExistsError:   # two bookings in the same second with the same slug
                bid = f"{core.new_id(slug or note or 'book', t)}-{secrets.token_hex(2)}"
                b["id"] = bid
                data = dumps(b)
        else:
            raise Refused("could not find a free booking id")
    rev = hashlib.sha256(data).hexdigest()
    slot = claim_receipt(board, "booking", bid, rev)
    board.log("book", booking=bid, slot=slot, gpus=core.gpu_text(gpus), start=start, end=end, revision=rev[:12])
    rows = evaluate(board, now(), entries=entries)
    me = next((r for r in rows if r["id"] == bid), None) or {}
    others = [r for r in rows if r["id"] != bid and r.get("state") in ("pending", "standing") and overlap(r, b)]
    return dict(id=bid, slot=slot, revision=rev, state=me.get("state"), why=me.get("why"),
                objection_close=me.get("objection_close"), start=b["start"], end=b["end"],
                overlaps=[dict(id=o["id"], user=o["user"], state=o["state"]) for o in others])


def record_arrival(board, booking_id, revision, entry_id, run, t=None):
    """The owner's persisted fact that a run of theirs was on this revision of the booking by its no-show limit, so a
    reader after the run ended does not read it as lapsed. Bound to the revision (arrivals/<id>@<revision16>.json): a
    rescheduled booking is a new revision and needs its own arrival."""
    board.writable()
    fd = board.dir("arrivals")
    if fd is None:
        return False
    key = f"{core.component(booking_id, 'booking id')}@{revision[:16]}"
    rec = dict(id=booking_id, key=key, revision=revision, user=core.USER, entry=entry_id, run=run,
               arrived=int(t or now()), schema=core.SCHEMA)
    try:
        core.publish(fd, f"{key}.json", dumps(rec))
    except FileExistsError:
        pass
    return True


def publish_objection(board, booking_id, reason):
    board.writable()
    core.component(booking_id, "booking id")
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise Refused("an objection needs a reason (--reason)")
    if len(reason) > core.REASON_MAX:
        raise Refused(f"reason: at most {core.REASON_MAX} characters")
    rows = evaluate(board, now(), entries=entries_with_occupancy(board))
    b = next((r for r in rows if r["id"] == booking_id), None)
    if b is None:
        raise Refused(f"no booking {booking_id}")
    if b["user"] == core.USER:
        raise Refused("you cannot object to your own booking; `bus unbook` withdraws it")
    if b.get("state") not in ("pending", "objected", "conflicting") or now() >= b.get("objection_close", 0):
        raise Refused(f"{booking_id} is {b.get('state')}: an objection counts only while its objection window is open")
    oid = core.new_id(f"obj-{booking_id[-12:]}")
    o = dict(id=oid, booking=booking_id, booking_revision=b["revision"], user=core.USER, reason=reason, created=now())
    fd = board.wdir("objections")
    data = dumps(o)
    core.publish(fd, f"{oid}.json", data)
    slot = claim_receipt(board, "objection", oid, hashlib.sha256(data).hexdigest())
    board.log("object", booking=booking_id, objection=oid, slot=slot)
    return dict(id=oid, slot=slot, booking=booking_id, revision=b["revision"])


def withdraw_objection(board, oid):
    board.writable()
    core.component(oid, "objection id")
    fd = board.wdir("objections")
    if not core.unlink_own(fd, f"{oid}.json"):
        raise Refused(f"no objection {oid} of yours")
    for slot, r in board.receipts().items():
        if r["id"] == oid and r["user"] == core.USER:
            core.unlink_own(board.dir("seq", str(slot // 10000)), str(slot))
    board.log("withdraw", objection=oid)


def unbook(board, bid):
    board.writable()
    core.component(bid, "booking id")
    if not core.unlink_own(board.wdir("bookings"), f"{bid}.json"):
        raise Refused(f"no booking {bid} of yours")
    board.log("unbook", booking=bid)
