"""Regression tests for the third review round: each reproduces the scenario the review described."""
import json, os, signal, subprocess, time, unittest

from helpers import SCALE, BusTest, FakeBoard, need_systemd, wait_for
from agentbus import bookings as bk
from agentbus import core, runner
from test_bookings import H, T0, booking, receipt, states

SLEEP = "sleep 300"


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@need_systemd
class HandoffRecovery(BusTest):
    def exclusive_run(self, gpu="4"):
        t = int(time.time())
        bid = self.bus_json("book", "--gpus", gpu, "--from", str(t), "--to", str(t + 3600), "--objection", "1s",
                            "--json")["id"]
        wait_for(lambda: any(b["id"] == bid and b["state"] == "standing" for b in self.bus_json("cal", "--json")[
            "bookings"]), 15, what="standing")
        return bid

    def crash_to(self, rid, **state):
        os.kill(self.state(rid)["supervisor"]["pid"], signal.SIGKILL)
        time.sleep(0.5)
        p = self.home / "state" / "runs" / f"{rid}.json"
        s = json.loads(p.read_text())
        s.update(state)
        p.write_text(json.dumps(s))

    def test_retry_after_a_pre_start_crash_rechecks_occupancy(self):
        off = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(off)
        self.set_gpus(apps=[(4, off.pid)])
        bid = self.exclusive_run()
        d = self.start(SLEEP, "4", "--scheduled", bid, "--exclusive")
        self.wait_state(d["id"], "waiting-handoff", 15)
        # the crash right after handoff_starting was saved and before the unit existed
        self.crash_to(d["id"], handoff_starting=True)
        self.bus_json("adopt", "--all")
        time.sleep(3)
        st = self.state(d["id"])
        self.assertEqual(st["state"], "waiting-handoff")
        self.assertTrue(any("process" in w for w in st.get("handoff_waits") or []), st.get("handoff_waits"))
        self.assertEqual(runner.main_unit_state(st["units"]["main"]), "absent")
        self.set_gpus()
        self.wait_state(d["id"], "running", 20)

    def test_booking_removed_after_a_post_start_crash_is_a_verified_release(self):
        pidf = self.tmp / "job.pid"
        bid = self.exclusive_run()
        d = self.start(f"echo $$ > {pidf}; exec sleep 300", "4", "--scheduled", bid, "--exclusive")
        jpid = wait_for(lambda: int(pidf.read_text()) if pidf.exists() and pidf.read_text().strip() else None, 30,
                        what="the job started")
        self.crash_to(d["id"], state="waiting-handoff", entry_state="waiting-handoff", handoff_starting=True)
        self.bus("unbook", bid, check=True)
        self.set_gpus(fail=True)                 # nvidia-smi does not answer
        self.bus_json("adopt", "--all")
        st = wait_for(lambda: (lambda x: x if x.get("release") else None)(self.state(d["id"])), 20,
                      what="a release started")
        self.assertEqual(st["release"]["reason"], "the booking ended while the GPUs were being handed over")
        time.sleep(4)
        st = self.state(d["id"])
        self.assertNotEqual(st["state"], "ended")                 # never released while nvidia-smi fails
        self.assertFalse(alive(jpid))
        self.set_gpus()
        st = self.wait_state(d["id"], "ended", 30)
        self.assertEqual(st["end_reason"], "the booking ended while the GPUs were being handed over")


class ConsequentialReads(BusTest):
    board_settings = dict(min_objection_s=1, default_objection_s=2)

    def test_a_decision_reads_every_receipt_fresh_and_replaces_a_stale_cache(self):
        t = int(time.time())
        bid = self.bus_json("book", "--gpus", "0", "--from", str(t), "--to", str(t + 3600), "--objection", "1s",
                            "--json")["id"]
        time.sleep(3)          # well past the 1 s objection window, whole seconds included
        with core.Board(str(self.root)) as b:
            good = b.receipts({})
        stale = {}
        for slot, r in good.items():      # a cache whose entry for the current key holds another revision
            bad = dict(r, revision="0" * 64)
            st = os.stat(self.root / "seq" / "0" / str(slot))
            stale[(slot, (st.st_ino, st.st_ctime_ns, st.st_size))] = bad
        sup = runner.Supervisor(f"{core.USER}-20261124-000000-x")
        sup.cache = dict(stale)
        sup.s = dict(state="running", **{"class": "backfill"}, requests={}, gpus=[0], booking=None, lapsed=[])
        old_ns = time.time_ns()
        try:
            core.RACY_NS = 0              # as if the receipt were older than the racy window
            with core.Board(str(self.root)) as b:
                cached = bk.evaluate(b, record=False, seen={}, cache=dict(stale), entries=[])
                self.assertEqual(next(r for r in cached if r["id"] == bid)["state"], "unpublished")
                msgs = [dict(kind="request", request_id="f" * 32)]          # a decision is due
                rows = sup.board_rows(b, int(time.time()), [], msgs)
        finally:
            core.RACY_NS = 5 * 10 ** 9
        row = next(r for r in rows if r["id"] == bid)
        self.assertEqual(row["state"], "standing", (row, time.time()))
        self.assertNotEqual(sup.cache, stale)                              # the stale entry is gone
        self.assertTrue(all(v["revision"] != "0" * 64 for v in sup.cache.values()))
        self.assertLess(time.time_ns() - old_ns, 30 * 10 ** 9)


class RevisionBound(unittest.TestCase):
    def test_a_remembered_lapse_of_an_old_revision_does_not_lapse_the_rescheduled_one(self):
        old = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        new = booking("alice-1-a", "alice", [0], T0 + 25 * H, T0 + 28 * H, previous=old["_data"])
        rs = {1: receipt(1, old), 2: receipt(2, new, created=T0 + 6 * H)}
        live = [dict(id="alice-x", user="alice", booking="alice-1-a", state="running", occupancy="live",
                     started=T0 + 25 * H + 60, booking_revision=new["_sha"])]
        s = states(FakeBoard([new], [], rs), T0 + 26 * H, entries=live,
                   sticky_lapsed={f"alice-1-a@{old['_sha']}"})
        self.assertEqual(s["alice-1-a"]["state"], "standing")

    def test_a_live_run_of_the_old_revision_does_not_satisfy_the_rescheduled_one(self):
        old = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        new = booking("alice-1-a", "alice", [0], T0 + 25 * H, T0 + 28 * H, previous=old["_data"])
        rs = {1: receipt(1, old), 2: receipt(2, new, created=T0 + 6 * H)}
        live = [dict(id="alice-x", user="alice", booking="alice-1-a", state="running", occupancy="live",
                     started=T0 + H + 60, booking_revision=old["_sha"])]       # started for yesterday's window
        s = states(FakeBoard([new], [], rs), T0 + 26 * H, entries=live)
        self.assertEqual(s["alice-1-a"]["state"], "lapsed")


@need_systemd
class PartialHold(BusTest):
    def test_one_ambiguous_pid_holds_only_its_gpu(self):
        d = self.start(SLEEP, "0-1")
        stray = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(stray)
        self.set_gpus(apps=[(0, stray.pid)])
        m = self.bus_json("send", d["id"], "--kind", "request", "--gpus", "1", "--minutes", "30", "--json")
        st = self.wait_state(d["id"], "needs-reconcile", 40 * SCALE)
        self.assertEqual(st["held_gpus"], [0])
        self.assertEqual(st["gpus_released"], [1])
        self.assertEqual(json.loads(self.entry_file(d["id"]).read_text())["gpus"], [0])
        ev = [n for n in st["notices"] if n["event"] == "needs-reconcile"][-1]
        self.assertEqual((ev["pids"], ev["gpus_held"], ev["gpus_released"]), ([stray.pid], [0], [1]))
        replies = [json.loads(p.read_text()) for p in (self.root / "inbox" / m["reply_to"]).glob("*.json")]
        self.assertTrue(any("GPUs 1 free of this job" in r.get("note", "") for r in replies), replies)
        # an exclusive booking's handoff on GPU 1 no longer waits on this run
        with core.Board(str(self.root)) as b:
            entries = bk.entries_with_occupancy(b)
        self.assertEqual(runner.handoff_waits(None, entries, dict(id="x", gpus=[1])), [])
        self.assertTrue(runner.handoff_waits(None, entries, dict(id="x", gpus=[0])))
        self.bus("reconcile", d["id"], "--confirm", str(stray.pid), check=True)
        self.wait_state(d["id"], "ended", 20)


if __name__ == "__main__":
    unittest.main()
