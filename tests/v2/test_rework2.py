"""Regression tests for the second review round: each reproduces the scenario the review described."""
import hashlib, json, os, signal, subprocess, time, unittest
from unittest import mock

from helpers import LINUX, REPO, SCALE, BusTest, FakeBoard, need_systemd, wait_for
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


# ------------------------------------------------------------------------------------------------ item 1 (P0-1)

@need_systemd
class ConservativeRelease(BusTest):
    def test_escape_whose_parent_exited_before_the_scan_holds_the_gpus(self):
        """The job's child leaves the slice and its parent exits before any pass sees it; the slice then empties. A
        readable outside cgroup must not make it 'other': the job pid is on the GPU, so nothing is released."""
        pidf = self.tmp / "orphan.pid"
        info = self.tmp / "i.json"
        stall = self.tmp / "stall"
        stall.touch()                      # no pass runs until the parent has exited
        env = dict(self.env, AGENT_BUS_TEST_STALL=str(stall))
        self.bus("run", "--gpus", "0", "--info-file", str(info), "--detach", "--",
                 f"systemd-run --user --quiet --scope --unit={self.prefix}-orph -- bash -c 'echo $$ > {pidf}; "
                 f"exec sleep 300' & sleep 1; exit 0", env=env, check=True)
        rid = json.loads(info.read_text())["id"]
        child = wait_for(lambda: int(pidf.read_text()) if pidf.exists() and pidf.read_text().strip() else None, 20,
                         what="the orphaned child")
        self.set_gpus(apps=[(0, child)])
        main = json.loads(info.read_text())["main_unit"]
        wait_for(lambda: int(runner.unit_props(main, "ExecMainExitTimestampMonotonic").get(
            "ExecMainExitTimestampMonotonic") or 0) > 0, 20, what="the leader exited")
        self.assertNotEqual(runner._ppid(child), os.getpid())
        stall.unlink()
        st = self.wait_state(rid, "needs-reconcile", 30)
        self.assertNotIn(str(child), st.get("escaped") or {})      # never seen by ancestry: still not released
        self.assertEqual(st["needs_reconcile"], [child])
        self.assertTrue(self.entry_file(rid).exists())
        self.assertEqual(json.loads(self.entry_file(rid).read_text())["state"], "needs-reconcile")
        self.assertIn("needs-reconcile", [n["event"] for n in st["notices"]])
        subprocess.run(["systemctl", "--user", "kill", "--signal=KILL", f"{self.prefix}-orph.scope"])
        wait_for(lambda: not alive(child), 10, what="the child gone")
        self.assertEqual(self.wait_state(rid, "ended", 30)["state"], "ended")

    def test_process_of_another_live_run_is_attributed_and_does_not_hold(self):
        a = self.start(SLEEP, "2")
        pidf = self.tmp / "b.pid"
        b = self.start(f"echo $$ > {pidf}; exec sleep 300", "3")
        bpid = wait_for(lambda: int(pidf.read_text()) if pidf.exists() and pidf.read_text().strip() else None, 10,
                        what="run b's pid")
        self.set_gpus(apps=[(2, bpid)])          # run b's process shows on run a's GPU
        r = self.bus_json("release", a["id"], "--wait", str(40 * SCALE), "--json")
        self.assertTrue(r["released"], r)
        self.assertTrue(any(b["id"] in x for x in self.state(a["id"])["_other_on_gpus"]))

    def test_unattributed_process_of_ours_on_the_gpu_needs_reconcile_after_release(self):
        d = self.start(SLEEP, "1")
        stray = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(stray)
        self.set_gpus(apps=[(1, stray.pid)])
        self.bus("release", d["id"], check=True)
        st = self.wait_state(d["id"], "needs-reconcile", 40)
        self.assertEqual(st["needs_reconcile"], [stray.pid])
        self.assertIsNone(stray.poll(), "an unattributed process is never signalled")
        r = self.bus("reconcile", d["id"], "--confirm", "999999")
        self.assertNotEqual(r.returncode, 0)          # only a pid that is waiting can be confirmed
        self.bus("reconcile", d["id"], "--confirm", str(stray.pid), check=True)
        self.wait_state(d["id"], "ended", 20)

    def test_unit_signalled_whole_only_on_its_full_membership(self):
        """The job's child sits in a sub-cgroup of a delegated scope; an unrelated process sits in another sub-cgroup
        of the same scope. The recorded child's subtree is all job, the unit is not: the unit must not be signalled."""
        pidf = self.tmp / "c.pid"
        unit = f"{self.prefix}-deleg"
        d = self.start(f"systemd-run --user --quiet --scope -p Delegate=yes --unit={unit} -- bash -c "
                       f"'cg=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup); mkdir $cg/jobpart $cg/other; "
                       f"echo $$ > $cg/jobpart/cgroup.procs; echo $$ > {pidf}; exec sleep 300' & sleep 300")
        child = wait_for(lambda: int(pidf.read_text()) if pidf.exists() and pidf.read_text().strip() else None, 20,
                         what="the delegated child")
        wait_for(lambda: str(child) in (self.state(d["id"]).get("escaped") or {}), 10, what="recorded")
        cg = core.proc_cgroup(child)
        unit_cg = cg.rsplit("/", 1)[0]
        other = subprocess.Popen(["systemd-run", "--user", "--quiet", "--scope", f"--unit={self.prefix}-o2", "--",
                                  "sleep", "300"], start_new_session=True)
        self.procs.append(other)
        wait_for(lambda: f"{self.prefix}-o2" in (core.proc_cgroup(other.pid) or ""), 10, what="the other scope")
        with open(f"/sys/fs/cgroup{unit_cg}/other/cgroup.procs", "w") as f:
            f.write(str(other.pid))
        r = self.bus_json("release", d["id"], "--wait", str(40 * SCALE), "--json")
        self.assertTrue(r["released"], r)
        self.assertFalse(alive(child))
        self.assertIsNone(other.poll(), "a process in another part of the unit was signalled")


# ------------------------------------------------------------------------------------------------ item 4 (P0-4)

class ValidateFirst(BusTest):
    def test_supervise_refuses_a_bad_id_before_any_lock_file(self):
        outside = self.home / "state" / "x.lock"
        r = self.bus("supervise", "../x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("bad run id", r.stderr)
        self.assertFalse(outside.exists())
        self.assertFalse((self.home / "state" / "runs" / "..").resolve().joinpath("x.lock").exists())

    def test_reply_refuses_a_notice_whose_reply_to_is_a_path(self):
        box = self.bus("mailbox", "talk", check=True).stdout.strip()
        mid = f"{core.USER}-20261124-000000-n1"
        m = dict(id=mid, sender=core.USER, to=box, kind="notice", reply_to="../../escape", note="hi", schema=2,
                 created=int(time.time()))
        (self.root / "inbox" / box / f"{mid}.json").write_text(json.dumps(m))
        r = self.bus("reply", mid, "--answer", "yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no mailbox of its sender", r.stderr)
        self.assertFalse((self.root.parent / "escape").exists())

    def test_board_dir_refuses_path_parts(self):
        with core.Board(str(self.root)) as b:
            for bad in ("..", "a/b", "", "."):
                with self.assertRaises(core.Refused):
                    b.dir("inbox", bad)
            self.assertIsNone(b.inbox_owner("../x"))


# ------------------------------------------------------------------------------------------------ item 5 (P1-5)

@need_systemd
class HandoffCrash(BusTest):
    def test_replacement_supervisor_recognises_a_job_its_predecessor_started(self):
        off = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(off)
        self.set_gpus(apps=[(4, off.pid)])
        t = int(time.time())
        bid = self.bus_json("book", "--gpus", "4", "--from", str(t), "--to", str(t + 3600), "--objection", "1s",
                            "--json")["id"]
        wait_for(lambda: any(b["id"] == bid and b["state"] == "standing" for b in self.bus_json("cal", "--json")[
            "bookings"]), 15, what="standing")
        pidf = self.tmp / "job.pid"
        d = self.start(f"echo $$ > {pidf}; exec sleep 300", "4", "--scheduled", bid, "--exclusive")
        self.set_gpus()
        jpid = wait_for(lambda: int(pidf.read_text()) if pidf.exists() and pidf.read_text().strip() else None, 30,
                        what="the job started")
        st = self.state(d["id"])
        os.kill(st["supervisor"]["pid"], signal.SIGKILL)
        time.sleep(0.5)
        # the crash as it would have happened: main unit up, nothing of the handoff saved
        p = self.home / "state" / "runs" / f"{d['id']}.json"
        s = json.loads(p.read_text())
        s.update(state="waiting-handoff", entry_state="waiting-handoff", handoff_starting=True)
        s["units"]["cgroup_path"] = None
        p.write_text(json.dumps(s))
        a = runner.admit_read(d["id"])
        a["admission"] = "handoff"
        runner.admit_write(d["id"], a)
        self.set_gpus(apps=[(4, jpid)])           # the job's own process is on the booked GPU
        self.bus_json("adopt", "--all")
        st = self.wait_state(d["id"], "running", 20)
        self.assertEqual(runner.admit_read(d["id"])["admission"], "open")
        self.assertTrue(st["units"]["cgroup_path"])
        self.assertTrue(alive(jpid), "the job was not restarted or stopped")
        self.assertEqual(self.bus("exec", d["id"], "--run", d["run"], "--", "true").returncode, 0)


# ------------------------------------------------------------------------------------------------ item 6 (P1-6)

@need_systemd
class StartingWithFailedQuery(BusTest):
    def test_failed_main_unit_query_keeps_starting_and_release_unverified(self):
        d = self.start(SLEEP)
        st = self.state(d["id"])
        os.kill(st["supervisor"]["pid"], signal.SIGKILL)
        time.sleep(0.5)
        p = self.home / "state" / "runs" / f"{d['id']}.json"
        s = json.loads(p.read_text())
        s["state"] = "starting"
        s.pop("main_started", None)
        p.write_text(json.dumps(s))
        fail = self.tmp / "systemctl-fails"
        fail.touch()
        env = dict(self.env, AGENT_BUS_TEST_SYSTEMCTL_FAIL=str(fail))
        sup = self.bg("supervise", d["id"], "--adopt", env=env)
        time.sleep(3)
        self.assertEqual(self.state(d["id"])["state"], "starting")
        self.assertIsNone(sup.poll())
        r = self.bus("release", d["id"], "--wait", "3", "--json", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["released"])
        fail.unlink()
        st = wait_for(lambda: (lambda x: x if x["state"] in ("stopping", "running", "ended") else None)(
            self.state(d["id"])), 20, what="resolved")
        self.assertNotEqual(st.get("end_reason"), "its units never started")
        self.wait_state(d["id"], "ended", 40)
        self.assertTrue(self.state(d["id"])["end_reason"].startswith("released"))


# ------------------------------------------------------------------------------------------------ item 7 (P1-7)

class ReceiptsRacy(BusTest):
    board_settings = dict(min_objection_s=600, default_objection_s=1800)

    def test_forty_same_size_rewrites_in_place_are_all_seen(self):
        t = int(time.time())
        self.bus("book", "--gpus", "0", "--from", str(t + 3600), "--to", str(t + 7200), "--json", check=True)
        p = self.root / "seq" / "0" / "1"
        rec = json.loads(p.read_text())
        cache, seen = {}, set()
        for i in range(40):
            rec["id"] = rec["id"][:-2] + f"{i:02d}"     # same length every time
            with open(p, "r+") as f:
                f.seek(0)
                f.write(json.dumps(rec))
                f.truncate()
            with core.Board(str(self.root)) as b:
                got = b.receipts(cache)[1]
            self.assertEqual(got["id"], rec["id"])
            seen.add(got["_sha"])
        self.assertEqual(len(seen), 40)


class ChainAndArrival(unittest.TestCase):
    def test_second_narrowing_does_not_inherit_a_conflicting_grandparents_priority(self):
        a = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 6 * H)
        g = booking("bob-1-b", "bob", [1, 2], T0 + H, T0 + 6 * H)                    # conflicting with alice's
        p = booking("bob-1-b", "bob", [1, 2], T0 + 2 * H, T0 + 5 * H, previous=g["_data"])
        c = booking("bob-1-b", "bob", [2], T0 + 2 * H, T0 + 5 * H, previous=p["_data"])  # no longer overlaps alice
        rs = {1: receipt(1, a), 2: receipt(2, g), 3: receipt(3, p, created=T0 + H), 4: receipt(4, c, created=T0 + 2 * H)}
        s = states(FakeBoard([a, c], [], rs), T0 + 2 * H + 60)
        self.assertFalse(s["bob-1-b"].get("inherited"))
        self.assertEqual(s["bob-1-b"]["order"], 4)
        self.assertEqual(s["bob-1-b"]["state"], "pending")

    def test_yesterdays_arrival_does_not_satisfy_a_rescheduled_window(self):
        old = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        arr = dict(id="alice-1-a", user="alice", entry="alice-1-run", run="a" * 32, arrived=T0 + H + 60, schema=2,
                   revision=old["_sha"], key=f"alice-1-a@{old['_sha'][:16]}")
        new = booking("alice-1-a", "alice", [0], T0 + 25 * H, T0 + 28 * H, previous=old["_data"])  # moved to tomorrow
        rs = {1: receipt(1, old), 2: receipt(2, new, created=T0 + 6 * H)}
        s = states(FakeBoard([new], [], rs, arrivals=[arr]), T0 + 26 * H, entries=[])
        self.assertEqual(s["alice-1-a"]["state"], "lapsed")

    def test_arrival_of_the_inherited_revision_counts_for_a_narrowing(self):
        old = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 6 * H)
        arr = dict(id="alice-1-a", user="alice", entry="alice-1-run", run="a" * 32, arrived=T0 + H + 600, schema=2,
                   revision=old["_sha"], key=f"alice-1-a@{old['_sha'][:16]}")
        new = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H, previous=old["_data"])
        rs = {1: receipt(1, old), 2: receipt(2, new, created=T0 + 2 * H)}
        s = states(FakeBoard([new], [], rs, arrivals=[arr]), T0 + 4 * H, entries=[])
        self.assertTrue(s["alice-1-a"]["inherited"])
        self.assertEqual(s["alice-1-a"]["state"], "standing")


if __name__ == "__main__":
    unittest.main()
