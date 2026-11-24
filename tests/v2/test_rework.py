"""Regression tests for the round-1 review findings on the bus side (P0-1, P0-4, P1-5 ... P1-7)."""
import json, os, secrets, signal, subprocess, sys, time, unittest
from unittest import mock

from helpers import LINUX, PY, REPO, SCALE, BusTest, FakeBoard, need_systemd, wait_for
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


# ------------------------------------------------------------------------------------------------ P0-1

@need_systemd
class Escapes(BusTest):
    def scope_child(self, d, pidf, unit):
        return wait_for(lambda: int(pidf.read_text()) if pidf.exists() and pidf.read_text().strip() else None, 20,
                        what="the escaped child")

    def test_unrelated_process_in_the_same_unit_survives_the_release(self):
        pidf = self.tmp / "esc.pid"
        unit = f"{self.prefix}-shared"
        d = self.start(f"systemd-run --user --quiet --scope --unit={unit} -- bash -c 'echo $$ > {pidf}; exec sleep 300' "
                       f"& sleep 300")
        child = self.scope_child(d, pidf, unit)
        cg = core.proc_cgroup(child)
        # an unrelated process of ours, started under the user manager (so it may be moved within its delegated tree),
        # then moved into the unit the job's child sits in
        other = subprocess.Popen(["systemd-run", "--user", "--quiet", "--scope", f"--unit={self.prefix}-other", "--",
                                  "sleep", "300"], start_new_session=True)
        self.procs.append(other)
        wait_for(lambda: f"{self.prefix}-other" in (core.proc_cgroup(other.pid) or ""), 10, what="the other scope")
        with open(f"/sys/fs/cgroup{cg}/cgroup.procs", "w") as f:
            f.write(str(other.pid))
        self.assertEqual(core.proc_cgroup(other.pid), cg)
        wait_for(lambda: str(child) in (self.state(d["id"]).get("escaped") or {}), 10, what="the child recorded")
        self.assertNotIn(str(other.pid), self.state(d["id"]).get("escaped") or {})
        r = self.bus_json("release", d["id"], "--wait", str(40 * SCALE), "--json")
        self.assertTrue(r["released"], r)
        self.assertFalse(alive(child))
        self.assertIsNone(other.poll(), "the unrelated process in the same unit was signalled")

    def test_process_carrying_the_public_generation_is_not_the_jobs(self):
        d = self.start(SLEEP, "1")
        st = self.state(d["id"])
        env = dict(os.environ, AGENT_BUS_RUN=st["run"], AGENT_BUS_ID=d["id"])
        other = subprocess.Popen(["sleep", "300"], env=env, start_new_session=True)
        self.procs.append(other)
        self.set_gpus(apps=[(1, other.pid)])
        self.bus("release", d["id"], check=True)
        st = self.wait_state(d["id"], "needs-reconcile", 40)   # ours, on the GPU, attributed to nothing: held
        self.assertIsNone(other.poll(), "a process that only claims the generation was signalled")
        self.assertNotIn(str(other.pid), st.get("escaped") or {})
        self.bus("reconcile", d["id"], "--confirm", str(other.pid), check=True)
        self.wait_state(d["id"], "ended", 20)

    @unittest.skipUnless(LINUX, "pidfd")
    def test_pidfd_signal_refuses_a_changed_identity(self):
        p = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(p)
        ident = core.proc_identity(p.pid)
        self.assertFalse(runner.pidfd_signal(dict(ident, ticks=ident["ticks"] + 1), signal.SIGTERM))
        self.assertIsNone(p.poll())
        self.assertTrue(runner.pidfd_signal(ident, signal.SIGTERM))
        self.assertEqual(p.wait(10), -signal.SIGTERM)


# ------------------------------------------------------------------------------------------------ P0-4

class SetupAndIds(BusTest):
    def setup(self, *args):
        return subprocess.run(["bash", str(REPO / "setup-board.sh"), *args], capture_output=True, text=True)

    def test_setup_refuses_a_symlinked_parent(self):
        os.symlink(self.tmp, self.tmp / "link")
        r = self.setup(str(self.tmp / "link" / "b2"), "h", "--blocks", "1")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.tmp / "b2").exists())

    def test_setup_rerun_does_not_follow_a_planted_symlink_or_chmod_existing(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        os.chmod(target, 0o700)
        os.rename(self.root / "objections", self.tmp / "objections-aside")
        os.symlink(target, self.root / "objections")
        os.chmod(self.root / "bookings", 0o2770)
        r = self.setup(str(self.root), self.host(), "--blocks", "3")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a directory", r.stdout + r.stderr)
        self.assertEqual(oct(os.stat(target).st_mode & 0o7777), "0o700")
        self.assertEqual(oct(os.stat(self.root / "bookings").st_mode & 0o7777), "0o2770")

    def test_ids_with_separators_or_dots_refused(self):
        for bad in ("../x", "a/b", "..", ".", ""):
            self.assertNotEqual(self.bus("state", bad).returncode, 0, bad)
            self.assertNotEqual(self.bus("release", bad).returncode, 0, bad)
            self.assertNotEqual(self.bus("unbook", bad).returncode, 0, bad)
        self.assertIn("bad", self.bus("state", "../x").stderr)

    def test_touch_sets_the_mode_only_on_a_file_it_creates(self):
        hb = self.root / "agents" / ".heartbeat" / "mine"
        hb.write_text("")
        os.chmod(hb, 0o600)
        fd = core.walk(str(self.root / "agents" / ".heartbeat"))
        self.assertTrue(core.touch(fd, "mine"))
        self.assertEqual(oct(os.stat(hb).st_mode & 0o777), "0o600")
        self.assertTrue(core.touch(fd, "new"))
        self.assertEqual(oct(os.stat(hb.parent / "new").st_mode & 0o777), "0o644")
        os.close(fd)

    def test_mkdir_own_leaves_an_existing_directorys_mode(self):
        d = self.root / "inbox" / f"{core.USER}-mail"
        d.mkdir()
        os.chmod(d, 0o700)
        fd = core.mkdir_own(core.walk(str(self.root / "inbox")), d.name, 0o3775)
        self.assertEqual(oct(os.fstat(fd).st_mode & 0o7777), "0o700")


# ------------------------------------------------------------------------------------------------ P1-5

class Handoff(BusTest):
    def s(self, gpus=(4,)):
        return dict(id=f"{core.USER}-x-me", gpus=list(gpus))

    def test_stale_heartbeat_entry_still_blocks_the_handoff(self):
        e = dict(id="bob-1-x", user="bob", gpus=[4], state="running", occupancy="unknown")
        waits = runner.handoff_waits(None, [e], self.s())
        self.assertTrue(any("bob-1-x" in w and "occupancy unknown" in w for w in waits), waits)

    def test_unidentified_gpu_blocks_the_handoff(self):
        self.set_gpus(n=4)       # nvidia-smi lists GPUs 0-3 only
        waits = runner.handoff_waits(None, [], self.s((2, 5)))
        self.assertTrue(any("not identified" in w for w in waits), waits)
        self.set_gpus()
        self.assertEqual(runner.handoff_waits(None, [], self.s((2, 5))), [])


@need_systemd
class HandoffAdmission(BusTest):
    def test_exec_refused_while_waiting_for_the_handoff_then_admitted(self):
        off = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(off)
        self.set_gpus(apps=[(4, off.pid)])
        t = int(time.time())
        bid = self.bus_json("book", "--gpus", "4", "--from", str(t), "--to", str(t + 3600), "--objection", "1s",
                            "--json")["id"]
        wait_for(lambda: any(b["id"] == bid and b["state"] == "standing" for b in self.bus_json("cal", "--json")[
            "bookings"]), 15, what="standing")
        d = self.start(SLEEP, "4", "--scheduled", bid, "--exclusive")
        wait_for(lambda: self.state(d["id"])["state"] == "waiting-handoff", 15, what="waiting-handoff")
        self.assertEqual(runner.admit_read(d["id"])["admission"], "handoff")
        r = self.bus("exec", d["id"], "--run", d["run"], "--", "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("closed", r.stderr)
        self.set_gpus()
        wait_for(lambda: self.state(d["id"])["state"] == "running", 20, what="running")
        self.assertEqual(runner.admit_read(d["id"])["admission"], "open")
        self.assertEqual(self.bus("exec", d["id"], "--run", d["run"], "--", "true").returncode, 0)


# ------------------------------------------------------------------------------------------------ P1-6

class CgroupPathUnknown(unittest.TestCase):
    def test_no_path_is_never_gone(self):
        self.assertEqual(runner.slice_state(None), "unknown")
        self.assertEqual(runner.slice_state(""), "unknown")

    def test_failed_query_recovers_nothing(self):
        with mock.patch.object(runner, "sysctl", return_value=(1, "", "Failed to connect to bus")):
            self.assertIsNone(runner.recover_cgroup("agentbus-x.slice"))


@need_systemd
class CgroupPathRecovery(BusTest):
    def test_adopter_recovers_a_path_the_crashed_client_never_saved(self):
        d = self.start(SLEEP)
        st = self.state(d["id"])
        real = st["units"]["cgroup_path"]
        os.kill(st["supervisor"]["pid"], signal.SIGKILL)
        time.sleep(0.5)
        p = self.home / "state" / "runs" / f"{d['id']}.json"
        s = json.loads(p.read_text())
        s["units"]["cgroup_path"] = None
        s["state"] = "starting"
        s.pop("main_started", None)
        p.write_text(json.dumps(s))
        self.bus_json("adopt", "--all")
        st = wait_for(lambda: (lambda x: x if x["units"].get("cgroup_path") and x["state"] == "running" else None)(
            self.state(d["id"])), 20, what="the path recovered")
        self.assertEqual(st["units"]["cgroup_path"], real)
        self.assertEqual(runner.unit_props(d["main_unit"], "ActiveState")["ActiveState"], "active")
        self.assertTrue(self.bus_json("release", d["id"], "--wait", str(40 * SCALE), "--json")["released"])

    def test_unknown_path_keeps_a_release_unverified(self):
        d = self.start(SLEEP)
        st = self.state(d["id"])
        with mock.patch.object(runner, "sysctl", return_value=(1, "", "no bus")):
            self.assertIsNone(runner.recover_cgroup(st["units"]["slice"]))
        sup = runner.Supervisor(d["id"])
        sup.s = dict(st, units=dict(st["units"], cgroup_path=None))
        ok, remains = sup.verify()
        self.assertFalse(ok)
        self.assertTrue(any("path unknown" in x for x in remains), remains)


# ------------------------------------------------------------------------------------------------ P1-7

class ReceiptsAndInheritance(BusTest):
    board_settings = dict(min_objection_s=600, default_objection_s=1800)

    def test_receipt_rewritten_in_place_is_replaced_for_a_cached_reader(self):
        t = int(time.time())
        self.bus("book", "--gpus", "0", "--from", str(t + 3600), "--to", str(t + 7200), "--json", check=True)
        cache = {}
        with core.Board(str(self.root)) as b:
            first = b.receipts(cache)[1]["_sha"]
        p = self.root / "seq" / "0" / "1"
        ino = os.stat(p).st_ino
        rec = json.loads(p.read_text())
        rec["created"] -= 1
        with open(p, "r+") as f:      # same inode, new content
            f.seek(0)
            f.write(json.dumps(rec))
            f.truncate()
        self.assertEqual(os.stat(p).st_ino, ino)
        with core.Board(str(self.root)) as b:
            again = b.receipts(cache)[1]["_sha"]
        self.assertNotEqual(first, again)
        seen = {"1": dict(sha=first, first_seen=None)}
        with core.Board(str(self.root)) as b:
            rows = bk.evaluate(b, seen=seen, record=False, cache=cache, entries=[])
        self.assertEqual(rows[0]["state"], "invalid")


class InheritanceAndArrival(unittest.TestCase):
    def test_narrowing_a_conflicting_predecessor_does_not_inherit(self):
        a = booking("alice-1-a", "alice", [0, 1], T0 + H, T0 + 6 * H)
        b1 = booking("bob-1-b", "bob", [1, 2], T0 + H, T0 + 6 * H)               # conflicting with alice's
        b2 = booking("bob-1-b", "bob", [1], T0 + 2 * H, T0 + 5 * H, previous=b1["_data"])
        rs = {1: receipt(1, a), 2: receipt(2, b1), 3: receipt(3, b2, created=T0 + 2 * H)}
        s = states(FakeBoard([a, b2], [], rs), T0 + 2 * H + 60)
        self.assertFalse(s["bob-1-b"].get("inherited"))
        self.assertEqual(s["bob-1-b"]["state"], "conflicting")

    def test_narrowing_an_over_budget_predecessor_does_not_inherit(self):
        bs, rs = [], {}
        for i in range(4):
            x = booking(f"alice-{i}-a", "alice", list(range(8)), T0 + H + i * 4 * H, T0 + 4 * H + i * 4 * H)
            bs.append(x)
            rs[i + 1] = receipt(i + 1, x)
        p1 = booking("alice-9-z", "alice", list(range(8)), T0 + 20 * H, T0 + 23 * H)   # over budget
        p2 = booking("alice-9-z", "alice", [0], T0 + 20 * H, T0 + 21 * H, previous=p1["_data"])
        rs[5], rs[6] = receipt(5, p1), receipt(6, p2, created=T0 + 1)
        s = states(FakeBoard(bs + [p2], [], rs), T0 + 1801)
        self.assertFalse(s["alice-9-z"].get("inherited"))

    def test_on_time_arrival_keeps_a_finished_scheduled_booking_from_lapsing(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        arr = dict(id="alice-1-a", user="alice", entry="alice-1-run", run="a" * 32, arrived=T0 + H + 60, schema=2,
                   revision=b["_sha"], key=f"alice-1-a@{b['_sha'][:16]}")
        fb = FakeBoard([b], [], {1: receipt(1, b)}, arrivals=[arr])
        self.assertEqual(states(fb, T0 + 3 * H, entries=[])["alice-1-a"]["state"], "standing")
        late = dict(arr, arrived=T0 + H + 40 * 60)
        fb = FakeBoard([b], [], {1: receipt(1, b)}, arrivals=[late])
        self.assertEqual(states(fb, T0 + 3 * H, entries=[])["alice-1-a"]["state"], "lapsed")

    def test_arrival_by_another_user_does_not_count(self):
        b = booking("alice-1-a", "alice", [0], T0 + H, T0 + 5 * H)
        arr = dict(id="alice-1-a", user="bob", entry="bob-1-run", run="a" * 32, arrived=T0 + H + 60, schema=2,
                   revision=b["_sha"], key=f"alice-1-a@{b['_sha'][:16]}")
        fb = FakeBoard([b], [], {1: receipt(1, b)}, arrivals=[arr])
        self.assertEqual(states(fb, T0 + 3 * H, entries=[])["alice-1-a"]["state"], "lapsed")


@need_systemd
class ArrivalOnTheBoard(BusTest):
    board_settings = dict(min_objection_s=1, default_objection_s=2, lapse_s=3)

    def test_scheduled_run_records_its_arrival_and_the_booking_stays_standing_after_it_ends(self):
        t = int(time.time())
        bid = self.bus_json("book", "--gpus", "3", "--from", str(t), "--to", str(t + 3600), "--objection", "1s",
                            "--json")["id"]
        wait_for(lambda: any(b["id"] == bid and b["state"] == "standing" for b in self.bus_json("cal", "--json")[
            "bookings"]), 15, what="standing")
        d = self.start(SLEEP, "3", "--scheduled", bid)
        arr = wait_for(lambda: next(iter((self.root / "arrivals").glob(f"{bid}@*.json")), None), 15,
                       what="the arrival record")
        self.assertEqual(os.stat(arr).st_mode & 0o777, 0o644)
        self.bus_json("release", d["id"], "--wait", str(40 * SCALE), "--json")
        time.sleep(4)   # past the no-show limit with no run on the booking
        b = next(b for b in self.bus_json("cal", "--json")["bookings"] if b["id"] == bid)
        self.assertEqual(b["state"], "standing")


if __name__ == "__main__":
    unittest.main()
