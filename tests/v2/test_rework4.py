"""Regression tests for the fourth review round: each reproduces the scenario the review described."""
import json, os, signal, subprocess, time, unittest

from helpers import SCALE, BusTest, FakeBoard, need_systemd, wait_for
from agentbus import core, runner
from test_bookings import H, T0, booking, receipt, states

SLEEP = "sleep 300"


@need_systemd
class UnresolvedAcrossGpus(BusTest):
    def test_a_pid_that_moves_onto_a_released_gpu_keeps_the_run_held(self):
        d = self.start(SLEEP, "0-1")
        stray = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(stray)
        self.set_gpus(apps=[(0, stray.pid)])
        self.bus("release", d["id"], check=True)
        st = self.wait_state(d["id"], "needs-reconcile", 40 * SCALE)
        self.assertEqual((st["held_gpus"], st["gpus_released"]), ([0], [1]))
        self.set_gpus(apps=[(1, stray.pid)])         # the same unresolved process now on the released GPU
        st = wait_for(lambda: (lambda x: x if x.get("held_gpus") == [1] else None)(self.state(d["id"])), 20,
                      what="GPU 1 held again")
        self.assertEqual(st["state"], "needs-reconcile")
        r = self.bus("release", d["id"], "--wait", "3", "--json")
        self.assertFalse(json.loads(r.stdout)["released"])
        self.set_gpus()                              # off every GPU but still alive: still not released
        time.sleep(3)
        st = self.state(d["id"])
        self.assertEqual(st["state"], "needs-reconcile")
        self.assertIn(str(stray.pid), st["unresolved"])
        stray.kill()
        stray.wait()
        self.wait_state(d["id"], "ended", 20)


@need_systemd
class BoardUnavailableDuringHandoff(BusTest):
    def test_an_unreadable_board_after_a_post_start_crash_stops_nothing(self):
        t = int(time.time())
        bid = self.bus_json("book", "--gpus", "4", "--from", str(t), "--to", str(t + 3600), "--objection", "1s",
                            "--json")["id"]
        wait_for(lambda: any(b["id"] == bid and b["state"] == "standing" for b in self.bus_json("cal", "--json")[
            "bookings"]), 15, what="standing")
        pidf = self.tmp / "job.pid"
        d = self.start(f"echo $$ > {pidf}; exec sleep 300", "4", "--scheduled", bid, "--exclusive")
        jpid = wait_for(lambda: int(pidf.read_text()) if pidf.exists() and pidf.read_text().strip() else None, 30,
                        what="the job started")
        os.kill(self.state(d["id"])["supervisor"]["pid"], signal.SIGKILL)
        time.sleep(0.5)
        p = self.home / "state" / "runs" / f"{d['id']}.json"
        s = json.loads(p.read_text())
        s.update(state="waiting-handoff", entry_state="waiting-handoff", handoff_starting=True)
        p.write_text(json.dumps(s))
        os.rename(self.root, self.tmp / "board-away")          # the pinned board is not there
        self.bus_json("adopt", "--all")
        time.sleep(4)
        st = self.state(d["id"])
        self.assertIsNone(st.get("release"), st.get("release"))
        self.assertNotEqual(st["state"], "ended")
        os.kill(jpid, 0)                                          # the job still runs
        os.rename(self.tmp / "board-away", self.root)
        self.wait_state(d["id"], "running", 20)


class ArrivalCoverage(unittest.TestCase):
    def test_a_live_run_on_gpu4_does_not_satisfy_its_booking_moved_to_gpu5(self):
        b = booking("alice-1-a", "alice", [5], T0 + H, T0 + 5 * H)
        live = [dict(id="alice-x", user="alice", booking="alice-1-a", state="running", occupancy="live",
                     started=T0 + H + 60, booking_revision=b["_sha"], gpus=[4])]
        s = states(FakeBoard([b], [], {1: receipt(1, b)}), T0 + 3 * H, entries=live)
        self.assertEqual(s["alice-1-a"]["state"], "lapsed")
        live[0]["gpus"] = [5]
        s = states(FakeBoard([b], [], {1: receipt(1, b)}), T0 + 3 * H, entries=live)
        self.assertEqual(s["alice-1-a"]["state"], "standing")


@need_systemd
class ArrivalNotRecordedOffItsGpus(BusTest):
    def test_a_run_whose_booking_moved_to_other_gpus_records_no_arrival(self):
        t = int(time.time())
        bid = self.bus_json("book", "--gpus", "4", "--from", str(t + 30), "--to", str(t + 3600), "--objection", "1s",
                            "--json")["id"]
        d = self.start(SLEEP, "4", "--scheduled", bid)
        self.bus_json("book", "--revise", bid, "--gpus", "5", "--from", str(t + 30), "--to", str(t + 3600),
                      "--objection", "1s", "--json")
        time.sleep(35)                       # the window has started; the revision covers GPU 5 only
        self.assertEqual(list((self.root / "arrivals").glob(f"{bid}@*.json")), [])
        self.assertFalse(self.state(d["id"]).get("arrival_recorded"))


class BackfillSeesBookingsFresh(BusTest):
    board_settings = dict(min_objection_s=1, default_objection_s=2)

    def test_board_rows_ignore_any_cache_so_a_standing_booking_is_seen_at_once(self):
        t = int(time.time())
        bid = self.bus_json("book", "--gpus", "0", "--from", str(t), "--to", str(t + 3600), "--objection", "1s",
                            "--json")["id"]
        time.sleep(3)
        sup = runner.Supervisor(f"{core.USER}-20261124-000000-x")
        sup.s = dict(state="running", **{"class": "backfill"}, requests={}, gpus=[0], booking=None, lapsed=[])
        sup.cache = {(1, (0, 0, 0)): dict(slot=1, kind="booking", id="x", revision="0" * 64, user="x", created=0,
                                          _sha="0")}          # anything left in a cache is never consulted
        with core.Board(str(self.root)) as b:
            rows = sup.board_rows(b, int(time.time()), [], [])     # no request, no yield candidate yet
        self.assertEqual(next(r for r in rows if r["id"] == bid)["state"], "standing")


if __name__ == "__main__":
    unittest.main()
