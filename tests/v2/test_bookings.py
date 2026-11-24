"""Booking states, limits, receipts and the objection window (design 4.2), on a fake board and on a real one."""
import hashlib, json, os, threading, time, unittest

from helpers import BusTest, FakeBoard
from agentbus import bookings as bk
from agentbus import core

T0 = 1_800_000_000
H = 3600


def booking(bid, user, gpus, start, end, objection_s=1800, created=T0, **kw):
    b = dict(id=bid, user=user, gpus=gpus, start=start, end=end, created=created, objection_s=objection_s,
             kind="training", note="", source="cli", ref=None, schema=2, **kw)
    data = json.dumps(b, sort_keys=True)
    b["_sha"] = hashlib.sha256(data.encode()).hexdigest()
    b["_data"] = data
    return b


def receipt(slot, rec, kind="booking", created=None, sha=None):
    r = dict(slot=slot, kind=kind, id=rec["id"], revision=rec["_sha"], user=rec["user"],
             created=rec["created"] if created is None else created)
    r["_sha"] = sha or hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
    return r


def objection(oid, user, b, reason="I need these GPUs for a deadline run", created=T0):
    o = dict(id=oid, booking=b["id"], booking_revision=b["_sha"], user=user, reason=reason, created=created)
    o["_sha"] = hashlib.sha256(json.dumps(o, sort_keys=True).encode()).hexdigest()
    return o


def states(fb, t, **kw):
    return {r["id"]: r for r in bk.evaluate(fb, t, seen=kw.pop("seen", {}), record=False, **kw)}


def entry(bid, user, started, state="running", occupancy="live", revision=None):
    return dict(id=f"{user}-x", user=user, booking=bid, state=state, occupancy=occupancy, started=started,
                booking_revision=revision)


class States(unittest.TestCase):
    def one(self, b, t, objs=(), slot=1, **kw):
        fb = FakeBoard([b], objs, {slot: receipt(slot, b), **{10 + i: receipt(10 + i, o, "objection")
                                                              for i, o in enumerate(objs)}})
        return states(fb, t, **kw)[b["id"]]

    def test_pending_during_window(self):
        b = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 2 * H)
        self.assertEqual(self.one(b, T0 + 60)["state"], "pending")

    def test_standing_after_default_30_min(self):
        b = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 2 * H)
        r = self.one(b, T0 + 1800)
        self.assertEqual(r["state"], "standing")
        self.assertEqual(r["objection_close"], T0 + 1800)

    def test_reasoned_objection_in_window_objects(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 2 * H)
        o = objection("bob-1-o", "bob", b, created=T0 + 600)
        r = self.one(b, T0 + 3600, [o])
        self.assertEqual(r["state"], "objected")
        self.assertEqual(r["objections"][0]["user"], "bob")

    def test_objection_by_owner_ignored(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 2 * H)
        self.assertEqual(self.one(b, T0 + 3600, [objection("alice-1-o", "alice", b, created=T0 + 60)])["state"],
                         "standing")

    def test_objection_after_window_seen_late_does_not_count(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 2 * H)
        o = objection("bob-1-o", "bob", b, created=T0 + 60)  # backdated: claims to be early
        fb = FakeBoard([b], [o], {1: receipt(1, b), 2: receipt(2, o, "objection")})
        seen = {"2": dict(sha=receipt(2, o, "objection")["_sha"], first_seen=T0 + 1900)}  # this reader saw it arrive late
        self.assertEqual(states(fb, T0 + 2000, seen=seen)["alice-1-a"]["state"], "standing")

    def test_objection_bound_to_earlier_revision_ignored(self):
        b1 = booking("alice-1-a", "alice", [0], T0 + H, T0 + 2 * H)
        o = objection("bob-1-o", "bob", b1, created=T0 + 60)
        b2 = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 3 * H, created=T0)  # a rewrite (new sha)
        fb = FakeBoard([b2], [o], {1: receipt(1, b1), 2: receipt(2, o, "objection"), 3: receipt(3, b2, created=T0 + 100)})
        r = states(fb, T0 + 100 + 1800)["alice-1-a"]
        self.assertEqual(r["state"], "standing")
        self.assertEqual(r["objections"], [])

    def test_unpublished_without_receipt(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 2 * H)
        self.assertEqual(states(FakeBoard([b], [], {}), T0 + 5000)["alice-1-a"]["state"], "unpublished")

    def test_rewrite_without_receipt_is_unpublished(self):
        b1 = booking("alice-1-a", "alice", [0], T0 + H, T0 + 2 * H)
        b2 = booking("alice-1-a", "alice", [0], T0 + H, T0 + 3 * H)
        self.assertEqual(states(FakeBoard([b2], [], {1: receipt(1, b1)}), T0 + 5000)["alice-1-a"]["state"], "unpublished")

    def test_invalid_8_days_ahead(self):
        b = booking("alice-1-a", "alice", [0], T0 + 8 * 86400, T0 + 8 * 86400 + H)
        self.assertEqual(self.one(b, T0 + 5000)["state"], "invalid")

    def test_invalid_25_hours(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 26 * H)
        self.assertEqual(self.one(b, T0 + 5000)["state"], "invalid")

    def test_invalid_short_objection_window(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 2 * H, objection_s=300)
        self.assertEqual(self.one(b, T0 + 5000)["state"], "invalid")

    def test_conflicting_later_overlap_of_another_user(self):
        a = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 3 * H)
        b = booking("bob-1-b", "bob", [1, 2], T0 + 2 * H, T0 + 4 * H)
        s = states(FakeBoard([a, b], [], {1: receipt(1, a), 2: receipt(2, b)}), T0 + 5000)
        self.assertEqual(s["alice-1-a"]["state"], "standing")
        self.assertEqual(s["bob-1-b"]["state"], "conflicting")

    def test_receipt_order_not_created_field_decides(self):
        a = booking("alice-1-a", "alice", [0], T0 + H, T0 + 3 * H, created=T0 + 500)
        b = booking("bob-1-b", "bob", [0], T0 + H, T0 + 3 * H, created=T0)   # claims to be older
        s = states(FakeBoard([a, b], [], {1: receipt(1, a), 2: receipt(2, b)}), T0 + 5000)
        self.assertEqual(s["bob-1-b"]["state"], "conflicting")

    def test_objected_earlier_booking_does_not_block(self):
        a = booking("alice-1-a", "alice", [0], T0 + H, T0 + 3 * H)
        b = booking("bob-1-b", "bob", [0], T0 + H, T0 + 3 * H)
        o = objection("carol-1-o", "carol", a, created=T0 + 10)
        s = states(FakeBoard([a, b], [o], {1: receipt(1, a), 2: receipt(2, b), 3: receipt(3, o, "objection")}),
                   T0 + 5000)
        self.assertEqual(s["alice-1-a"]["state"], "objected")
        self.assertEqual(s["bob-1-b"]["state"], "standing")

    def test_same_user_overlap_not_conflicting(self):
        a = booking("alice-1-a", "alice", [0], T0 + H, T0 + 3 * H)
        b = booking("alice-2-b", "alice", [0], T0 + 2 * H, T0 + 4 * H)
        s = states(FakeBoard([a, b], [], {1: receipt(1, a), 2: receipt(2, b)}), T0 + 5000)
        self.assertEqual({s[k]["state"] for k in s}, {"standing"})

    def test_over_budget_past_96_gpu_hours(self):
        bs, rs = [], {}
        for i in range(5):   # 5 bookings x 8 GPUs x 3 h = 120 GPU-h within 7 days
            b = booking(f"alice-{i}-a", "alice", list(range(8)), T0 + H + i * 10 * H, T0 + 4 * H + i * 10 * H)
            bs.append(b)
            rs[i + 1] = receipt(i + 1, b)
        s = states(FakeBoard(bs, [], rs), T0 + 5000)
        got = [s[f"alice-{i}-a"]["state"] for i in range(5)]
        self.assertEqual(got, ["standing"] * 4 + ["over-budget"])

    def test_budget_ignores_objected_and_conflicting(self):
        bs, rs, objs = [], {}, []
        for i in range(5):
            b = booking(f"alice-{i}-a", "alice", list(range(8)), T0 + H + i * 10 * H, T0 + 4 * H + i * 10 * H)
            bs.append(b)
            rs[i + 1] = receipt(i + 1, b)
        o = objection("bob-1-o", "bob", bs[0], created=T0 + 60)
        rs[20] = receipt(20, o, "objection", created=T0 + 60)
        s = states(FakeBoard(bs, [o], rs), T0 + 5000)
        self.assertEqual(s["alice-0-a"]["state"], "objected")
        self.assertEqual(s["alice-4-a"]["state"], "standing")

    def test_lapsed_without_run_31_min_after_start(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        self.assertEqual(self.one(b, T0 + H + 31 * 60, entries=[])["state"], "lapsed")

    def test_not_lapsed_with_run_started_in_time(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        r = self.one(b, T0 + H + 31 * 60, entries=[entry("alice-1-a", "alice", T0 + H + 60, revision=b["_sha"])])
        self.assertEqual(r["state"], "standing")

    def test_waiting_handoff_counts_for_no_show(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        r = self.one(b, T0 + H + 40 * 60, entries=[entry("alice-1-a", "alice", T0 + H + 60, "waiting-handoff",
                                                          revision=b["_sha"])])
        self.assertEqual(r["state"], "standing")

    def test_stale_heartbeat_run_does_not_count(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        r = self.one(b, T0 + H + 40 * 60, entries=[entry("alice-1-a", "alice", T0 + H, occupancy="unknown")])
        self.assertEqual(r["state"], "lapsed")

    def test_sticky_lapsed(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        r = self.one(b, T0 + H + 40 * 60, entries=[entry("alice-1-a", "alice", T0 + H + 35 * 60, revision=b["_sha"])],
                     sticky_lapsed={f"alice-1-a@{b['_sha']}"})
        self.assertEqual(r["state"], "lapsed")

    def chain(self, new_gpus, new_start, new_end):
        b1 = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 6 * H)
        b2 = booking("alice-1-a", "alice", new_gpus, new_start, new_end, previous=b1["_data"])
        rs = {1: receipt(1, b1), 2: receipt(2, b2, created=T0 + 2 * H)}
        return states(FakeBoard([b2], [], rs), T0 + 2 * H + 60)["alice-1-a"]

    def test_narrowing_revision_keeps_standing(self):
        r = self.chain([0], T0 + 2 * H, T0 + 5 * H)
        self.assertEqual(r["state"], "standing")
        self.assertTrue(r["inherited"])

    def test_widening_revision_reopens_window(self):
        r = self.chain([0, 1, 2], T0 + 2 * H, T0 + 5 * H)
        self.assertEqual(r["state"], "pending")

    def test_forged_previous_not_inherited(self):
        b1 = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 6 * H)
        fake = booking("alice-1-a", "alice", [0, 1, 2, 3], T0 + H, T0 + 6 * H)   # never had a receipt
        b2 = booking("alice-1-a", "alice", [0, 1, 2], T0 + 2 * H, T0 + 5 * H, previous=fake["_data"])
        rs = {1: receipt(1, b1), 2: receipt(2, b2, created=T0 + 2 * H)}
        self.assertEqual(states(FakeBoard([b2], [], rs), T0 + 2 * H + 60)["alice-1-a"]["state"], "pending")

    def test_backdated_created_cannot_reach_before_previous_receipt(self):
        a = booking("alice-1-a", "alice", [0], T0 + 3 * H, T0 + 4 * H, created=T0 + 1000)
        b = booking("bob-1-b", "bob", [5], T0 + 3 * H, T0 + 4 * H, created=T0)   # backdated by 1000 s
        rs = {1: receipt(1, a), 2: receipt(2, b)}
        s = states(FakeBoard([a, b], [], rs), T0 + 1000 + 1801)
        self.assertEqual(s["bob-1-b"]["receipt_time"], T0 + 1000)
        self.assertEqual(s["bob-1-b"]["state"], "standing")
        self.assertEqual(states(FakeBoard([a, b], [], rs), T0 + 1000 + 1799)["bob-1-b"]["state"], "pending")

    def test_backdated_created_does_not_shorten_window_for_a_reader_that_saw_it(self):
        b = booking("bob-1-b", "bob", [5], T0 + 3 * H, T0 + 4 * H, created=T0)
        r1 = receipt(1, b)
        seen = {"1": dict(sha=r1["_sha"], first_seen=T0 + 900)}
        s = states(FakeBoard([b], [], {1: r1}), T0 + 1900, seen=seen)
        self.assertEqual(s["bob-1-b"]["objection_close"], T0 + 900 + 1800)
        self.assertEqual(s["bob-1-b"]["state"], "pending")

    def test_fresh_reader_uses_sequence_time(self):
        b = booking("bob-1-b", "bob", [5], T0 + 3 * H, T0 + 4 * H, created=T0)
        s = states(FakeBoard([b], [], {1: receipt(1, b)}), T0 + 1801, seen={})
        self.assertEqual(s["bob-1-b"]["state"], "standing")

    def test_replaced_slot_is_invalid_for_a_reader_that_saw_the_original(self):
        a = booking("alice-1-a", "alice", [0], T0 + H, T0 + 3 * H)
        old = receipt(1, booking("bob-0-z", "bob", [7], T0, T0 + H))
        b = booking("bob-1-b", "bob", [0], T0 + H, T0 + 3 * H)
        new1 = receipt(1, b)   # bob deleted his old low receipt and reused slot 1 to jump ahead of alice
        rs = {1: new1, 2: receipt(2, a)}
        seen = {"1": dict(sha=old["_sha"], first_seen=None), "2": dict(sha=rs[2]["_sha"], first_seen=None)}
        s = states(FakeBoard([a, b], [], rs), T0 + 5000, seen=seen)
        self.assertEqual(s["bob-1-b"]["state"], "invalid")
        self.assertEqual(s["alice-1-a"]["state"], "standing")
        s2 = states(FakeBoard([a, b], [], rs), T0 + 5000, seen={})    # the remaining limit: a reader that never saw it
        self.assertEqual(s2["alice-1-a"]["state"], "conflicting")

    def test_standing_active_on_excludes_own_and_respects_window(self):
        a = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 3 * H)
        rows = list(states(FakeBoard([a], [], {1: receipt(1, a)}), T0 + 2 * H).values())
        self.assertEqual([r["id"] for r in bk.standing_active_on(rows, [1], T0 + 2 * H)], ["alice-1-a"])
        self.assertEqual(bk.standing_active_on(rows, [1], T0 + 2 * H, exclude="alice-1-a"), [])
        self.assertEqual(bk.standing_active_on(rows, [2], T0 + 2 * H), [])
        self.assertEqual(bk.standing_active_on(rows, [1], T0 + 3 * H), [])

    def test_pending_booking_is_not_active(self):
        a = booking("alice-1-a", "alice", [0], T0, T0 + 3 * H)
        rows = list(states(FakeBoard([a], [], {1: receipt(1, a)}), T0 + 60).values())
        self.assertEqual(bk.standing_active_on(rows, [0], T0 + 60), [])

    def test_lapsed_booking_does_not_evict(self):
        a = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        rows = list(states(FakeBoard([a], [], {1: receipt(1, a)}), T0 + 3 * H, entries=[]).values())
        self.assertEqual(rows[0]["state"], "lapsed")
        self.assertEqual(bk.standing_active_on(rows, [0], T0 + 3 * H), [])


class Parsing(unittest.TestCase):
    def test_iso_with_offset_and_epoch(self):
        self.assertEqual(bk.parse_when("2026-11-24T22:00-05:00"), 1795575600)
        self.assertEqual(bk.parse_when("1795575600"), 1795575600)

    def test_naive_time_refused_in_round_1(self):
        with self.assertRaises(core.Refused):
            bk.parse_when("2026-11-24T22:00")
        with self.assertRaises(core.Refused):
            bk.parse_when("22:00")

    def test_durations(self):
        self.assertEqual(bk.parse_duration("30m"), 1800)
        self.assertEqual(bk.parse_duration("1h"), 3600)
        self.assertEqual(bk.parse_duration("600"), 600)


class Receipts(BusTest):
    board_settings = dict(min_objection_s=600, default_objection_s=1800)

    def book(self, *extra, gpus="0", start=None, end=None):
        t = int(time.time())
        return self.bus("book", "--gpus", gpus, "--from", str(start or t + 3600), "--to", str(end or t + 7200),
                        "--json", *extra)

    def test_book_gets_slot_1_then_2(self):
        a = json.loads(self.book().stdout)
        b = json.loads(self.book("--slug", "second").stdout)
        self.assertEqual((a["slot"], b["slot"]), (1, 2))
        self.assertEqual(a["state"], "pending")
        self.assertTrue((self.root / "seq" / "0" / "1").is_file())
        self.assertEqual(oct((self.root / "seq" / "0" / "1").stat().st_mode & 0o777), "0o644")

    def test_concurrent_publication_gets_distinct_slots(self):
        b = core.Board(str(self.root))
        slots, errs = [], []

        def claim(i):
            try:
                slots.append(bk.claim_receipt(core.Board(str(self.root)), "booking", f"{core.USER}-x-{i}", "0" * 64))
            except Exception as e:   # noqa
                errs.append(e)
        ts = [threading.Thread(target=claim, args=(i,)) for i in range(24)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        b.close()
        self.assertEqual(errs, [])
        self.assertEqual(sorted(slots), list(range(1, 25)))

    def test_junk_at_next_slot_is_skipped(self):
        (self.root / "seq" / "0" / "1").write_text("junk")
        r = json.loads(self.book().stdout)
        self.assertEqual(r["slot"], 2)

    def test_symlinked_block_ignored(self):
        os.rmdir(self.root / "seq" / "1")
        os.symlink(self.root / "seq" / "0", self.root / "seq" / "1")
        r = json.loads(self.book().stdout)
        with core.Board(str(self.root)) as b:
            self.assertEqual(set(b.receipts()), {r["slot"]})

    def test_chmod_and_touch_change_nothing(self):
        r = json.loads(self.book().stdout)
        before = self.bus_json("cal", "--json")["bookings"][0]
        p = self.root / "bookings" / f"{r['id']}.json"
        os.chmod(p, 0o600)
        os.utime(p, (time.time() + 999, time.time() + 999))
        os.chmod(p, 0o644)
        after = self.bus_json("cal", "--json")["bookings"][0]
        self.assertEqual((before["state"], before["slot"], before["revision"]), (after["state"], after["slot"],
                                                                               after["revision"]))

    def test_file_with_no_receipt_ignored(self):
        t = int(time.time())
        b = dict(id=f"{core.USER}-20990101-000000-x", user=core.USER, gpus=[0], start=t + 60, end=t + 3600, created=t,
                 objection_s=1800, kind="other", note="", source="cli", ref=None, schema=2)
        (self.root / "bookings" / f"{b['id']}.json").write_text(json.dumps(b))
        self.assertEqual(self.bus_json("cal", "--json")["bookings"], [])
        self.assertEqual(self.bus_json("cal", "--json", "--all")["bookings"][0]["state"], "unpublished")

    def test_tool_refuses_invalid_shapes(self):
        t = int(time.time())
        self.assertIn("7 days", self.book(start=t + 8 * 86400, end=t + 8 * 86400 + 3600).stderr)
        self.assertIn("24 h", self.book(start=t + 3600, end=t + 26 * 3600).stderr)
        self.assertIn("at least 10 min", self.book("--objection", "5m").stderr)
        self.assertIn("offset", self.bus("book", "--gpus", "0", "--from", "2030-01-01T10:00", "--to",
                                         "2030-01-01T11:00").stderr)

    def test_tool_refuses_over_budget(self):
        t = int(time.time()) + 3600
        for i in range(4):
            r = self.book("--slug", f"b{i}", gpus="0-7", start=t + i * 4 * 3600, end=t + i * 4 * 3600 + 3 * 3600)
            self.assertEqual(r.returncode, 0, r.stderr)
        r = self.book("--slug", "b5", gpus="0-7", start=t + 20 * 3600, end=t + 20 * 3600 + 1800)
        self.assertEqual(r.returncode, 1)
        self.assertIn("budget", r.stderr)

    def test_objection_needs_reason_and_not_own(self):
        r = json.loads(self.book().stdout)
        self.assertIn("own booking", self.bus("object", r["id"], "--reason", "x").stderr)
        self.assertIn("reason", self.bus("object", r["id"]).stderr)

    def test_revision_narrowing_keeps_standing_on_the_board(self):
        with open(self.root / "config", "a") as f:
            f.write("min_objection_s=1\n")
        t = int(time.time())
        r = json.loads(self.book("--objection", "1s", gpus="0-1", start=t + 600, end=t + 7200).stdout)
        wait = lambda st: next(b for b in self.bus_json("cal", "--json")["bookings"] if b["id"] == r["id"])["state"] == st
        from helpers import wait_for
        wait_for(lambda: wait("standing"), 10, what="standing")
        r2 = self.bus_json("book", "--revise", r["id"], "--gpus", "0", "--from", str(t + 900), "--to", str(t + 6000),
                           "--objection", "30m", "--json")
        self.assertEqual(r2["state"], "standing")
        r3 = self.bus_json("book", "--revise", r["id"], "--gpus", "0-2", "--from", str(t + 900), "--to", str(t + 6000),
                           "--objection", "30m", "--json")
        self.assertEqual(r3["state"], "pending")

    def test_unbook_own_only_and_gone(self):
        r = json.loads(self.book().stdout)
        self.bus("unbook", r["id"], check=True)
        self.assertEqual(self.bus_json("cal", "--json")["bookings"], [])
        self.assertIn("no booking", self.bus("unbook", r["id"]).stderr)

    def test_slots_seen_first_read_records_no_arrival_then_arrivals(self):
        json.loads(self.book().stdout)
        os.unlink(self.home / "state" / "slots.json")     # a reader that was not watching when slot 1 arrived
        self.bus_json("cal", "--json")
        seen = json.loads((self.home / "state" / "slots.json").read_text())["slots"]
        self.assertIsNone(seen["1"]["first_seen"])
        json.loads(self.book("--slug", "two").stdout)
        seen = json.loads((self.home / "state" / "slots.json").read_text())["slots"]
        self.assertIsNotNone(seen["2"]["first_seen"])


if __name__ == "__main__":
    unittest.main()
