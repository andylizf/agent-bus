"""Graphics-only GPU processes (Type G, such as EGL rendering) in the runner's GPU view: nvidia-smi's compute query
does not list them, so the view also reads the -q -d PIDS report, and an unreadable report makes the view unknown."""
import os, subprocess, time, unittest

from helpers import LINUX, SCALE, BusTest, need_systemd, wait_for
from agentbus import core, runner

SLEEP = "sleep 300"


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class GpuView(BusTest):
    def stray(self):
        p = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(p)
        return p

    def test_graphics_process_is_listed_on_its_gpu(self):
        p = self.stray()
        self.set_gpus(apps=[(0, os.getpid())], graphics=[(3, p.pid)])
        ok, idx, apps, _ = runner.nvidia_view()
        self.assertTrue(ok)
        self.assertEqual(sorted(apps), sorted([("GPU-0", os.getpid()), ("GPU-3", p.pid)]))

    def test_unreadable_or_incomplete_report_makes_the_view_unknown(self):
        self.set_gpus(pids_fail=True)
        self.assertFalse(runner.nvidia_view()[0])
        self.set_gpus(pids_missing=[5])
        self.assertFalse(runner.nvidia_view()[0])

    @unittest.skipUnless(LINUX, "process owners are read from /proc")
    def test_own_graphics_process_keeps_the_gpu_held_and_is_not_signalled(self):
        p = self.stray()
        self.set_gpus(graphics=[(1, p.pid)])
        clear, mine, other, err = runner.gpu_release_check(f"{core.USER}-gfx-none", [1])
        self.assertEqual((clear, mine, err), (False, [p.pid], None))
        self.assertIsNone(p.poll())
        self.set_gpus()
        self.assertTrue(runner.gpu_release_check(f"{core.USER}-gfx-none", [1])[0])

    def test_unreadable_report_never_reads_released(self):
        self.set_gpus(pids_fail=True)
        clear, mine, other, err = runner.gpu_release_check(f"{core.USER}-gfx-none", [1])
        self.assertFalse(clear)
        self.assertEqual(err, "nvidia-smi failed")

    def test_another_users_graphics_process_blocks_an_exclusive_handoff(self):
        s = dict(id=f"{core.USER}-x-me", gpus=[4])
        self.set_gpus(graphics=[(4, 1)])            # pid 1 belongs to root
        waits = runner.handoff_waits(None, [], s)
        self.assertTrue(any(w.startswith("process 1 ") for w in waits), waits)
        if LINUX:
            self.assertIn("process 1 (root)", waits)
        self.set_gpus(pids_fail=True)
        self.assertIn("nvidia-smi failed", runner.handoff_waits(None, [], s))
        self.set_gpus()
        self.assertEqual(runner.handoff_waits(None, [], s), [])


@need_systemd
class GraphicsRuns(BusTest):
    def book_now(self, gpus):
        t = int(time.time())
        return self.bus_json("book", "--gpus", gpus, "--from", str(t), "--to", str(t + 3600), "--objection", "1s",
                             "--json")["id"]

    def wait_booking(self, bid, state, timeout=15):
        return wait_for(lambda: next((b for b in self.bus_json("cal", "--json")["bookings"] if b["id"] == bid
                                      and b["state"] == state), None), timeout, what=f"{bid} {state}")

    def test_egl_only_process_of_ours_keeps_the_run_from_reading_released(self):
        d = self.start(SLEEP, "1")
        stray = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(stray)
        self.set_gpus(graphics=[(1, stray.pid)])    # listed only as a graphics process
        self.bus("release", d["id"], check=True)
        st = self.wait_state(d["id"], "needs-reconcile", 40)
        self.assertEqual(st["needs_reconcile"], [stray.pid])
        self.assertTrue(self.entry_file(d["id"]).exists())
        self.assertIsNone(stray.poll(), "a process not recorded as the run's is never signalled")

    def test_unreadable_report_keeps_a_releasing_run_on_the_board(self):
        d = self.start(SLEEP, "2")
        self.set_gpus(pids_fail=True)
        self.bus("release", d["id"], check=True)
        time.sleep(8 * SCALE)
        st = self.state(d["id"])
        self.assertNotEqual(st["state"], "ended")
        self.assertEqual(st.get("gpu_view"), "unknown")
        self.assertTrue(self.entry_file(d["id"]).exists())
        self.set_gpus()
        self.wait_state(d["id"], "ended", 40)

    def test_exclusive_handoff_waits_for_a_graphics_process(self):
        off = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(off)
        self.set_gpus(graphics=[(4, off.pid)])
        bid = self.book_now("4")
        self.wait_booking(bid, "standing")
        d = self.start(SLEEP, "4", "--scheduled", bid, "--exclusive")
        self.wait_state(d["id"], "waiting-handoff", 10)
        wait_for(lambda: any(f"process {off.pid}" in w for w in self.state(d["id"]).get("handoff_waits") or []), 10,
                 what="waits")
        self.set_gpus()
        self.wait_state(d["id"], "running", 15)


if __name__ == "__main__":
    unittest.main()
