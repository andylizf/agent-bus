"""Regression tests for the fifth review round: each reproduces the scenario the review described."""
import json, os, signal, subprocess, threading, time, unittest

from helpers import LINUX, PY, SCALE, BusTest, need_systemd, wait_for
from agentbus import core, runner

SLEEP = "sleep 300"


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# ------------------------------------------------------------------------------------------------ item 1

@need_systemd
class RunIdsAndLaunchTimeouts(BusTest):
    def test_two_same_slug_launches_in_one_second_get_distinct_ids(self):
        infos = [self.tmp / f"i{i}.json" for i in range(2)]
        procs = [self.bg("run", "--gpus", str(i), "--slug", "same", "--info-file", str(infos[i]), "--detach", "--",
                         SLEEP) for i in range(2)]
        for p in procs:
            self.assertEqual(p.wait(60), 0, p.stderr.read())
        ids = [json.loads(f.read_text())["id"] for f in infos]
        self.assertEqual(len(set(ids)), 2, ids)
        for i, rid in enumerate(ids):
            self.assertEqual(self.state(rid)["gpus"], [i])          # neither record overwrote the other
            self.assertEqual(os.stat(self.root / "inbox" / rid).st_mode & 0o7777, 0o3775)   # others can write to it
            wait_for(lambda: self.state(rid)["state"] == "running", 20, what=f"{rid} running")

    def test_a_launch_timeout_after_systemd_accepted_never_ends_the_job(self):
        env = dict(self.env, AGENT_BUS_TEST_MAIN_TIMEOUT="1")
        info = self.tmp / "i.json"
        r = self.bus("run", "--gpus", "0", "--info-file", str(info), "--detach", "--", SLEEP, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("systemd says the unit is exists", r.stderr)
        rid = json.loads(info.read_text())["id"]
        st = self.wait_state(rid, "running", 20)
        self.assertEqual(runner.unit_props(st["units"]["main"], "ActiveState")["ActiveState"], "active")
        rel = self.bus_json("release", rid, "--wait", str(40 * SCALE), "--json")
        self.assertTrue(rel["released"])
        self.assertTrue(self.state(rid)["end_reason"].startswith("released"))


# ------------------------------------------------------------------------------------------------ item 2

@need_systemd
class ExecLauncher(BusTest):
    def intents(self, rid):
        return runner.admit_read(rid)["inflight"]

    def test_a_dead_client_with_its_launcher_waiting_holds_the_release(self):
        d = self.start(SLEEP)
        pidf = self.tmp / "worker.pid"
        env = dict(self.env, AGENT_BUS_TEST_EXEC_PAUSE="launcher")
        client = self.bg("exec", d["id"], "--run", d["run"], "--", "bash", "-c", f"echo $$ > {pidf}; exec sleep 300",
                         env=env)
        it = wait_for(lambda: next((x for x in self.intents(d["id"]) if x.get("launcher")), None), 15,
                      what="the launcher recorded")
        lpid = it["launcher"]["pid"]
        os.kill(client.pid, signal.SIGKILL)             # the client alone dies; its launcher waits, not yet started
        client.wait()
        self.bus("release", d["id"], check=True)
        time.sleep(3)
        st = self.state(d["id"])
        self.assertEqual(st["state"], "stopping")       # the launcher lives: the intent is not cancelled
        self.assertEqual([x["state"] for x in self.intents(d["id"])], ["launching"])
        os.kill(lpid, signal.SIGCONT)                   # it starts its scope now
        st = self.wait_state(d["id"], "ended", 40 * SCALE)
        self.assertTrue(st["end_reason"].startswith("released"))
        if pidf.exists():
            self.assertFalse(alive(int(pidf.read_text())), "the late worker survived the release")

    def test_a_scope_after_cancellation_starts_nothing(self):
        d = self.start(SLEEP)
        marker = self.tmp / "ran"
        with runner.admit_lock(d["id"]):
            a = runner.admit_read(d["id"])
            a["inflight"].append(dict(exec_id="deadbeef0001", client=None, launcher=None, state="cancelled", t=0))
            runner.admit_write(d["id"], a)
        r = self.bus("_exec-guard", d["id"], "deadbeef0001", d["run"], "--", "touch", str(marker))
        self.assertEqual(r.returncode, 126)
        self.assertFalse(marker.exists())
        r = self.bus("_exec-guard", d["id"], "deadbeef0001", "0" * 32, "--", "touch", str(marker))
        self.assertEqual(r.returncode, 126)               # a stale generation is refused the same way
        self.assertFalse(marker.exists())


# ------------------------------------------------------------------------------------------------ item 3

@need_systemd
class FailedScanAndIdentities(BusTest):
    def test_a_failed_rescan_releases_no_gpu(self):
        d = self.start(SLEEP, "0-1")
        stray = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(stray)
        self.set_gpus(apps=[(0, stray.pid)])
        self.bus("release", d["id"], check=True)
        st = self.wait_state(d["id"], "needs-reconcile", 40 * SCALE)
        self.assertEqual(st["gpus_released"], [1])
        self.assertIn("gpus_released_checked", st)
        self.set_gpus(fail=True)                         # the next scan fails
        st = wait_for(lambda: (lambda x: x if x.get("gpus_released") == [] else None)(self.state(d["id"])), 20,
                      what="gpus_released emptied")
        self.assertNotIn("gpus_released_checked", st)
        self.set_gpus(apps=[(0, stray.pid)])
        st = wait_for(lambda: (lambda x: x if x.get("gpus_released") == [1] else None)(self.state(d["id"])), 20,
                      what="released again after a good scan")
        stray.kill()
        stray.wait()
        self.wait_state(d["id"], "ended", 20)

    @unittest.skipUnless(LINUX, "/proc")
    def test_a_confirmation_does_not_pass_to_a_process_that_reuses_the_pid(self):
        d = self.start(SLEEP, "1")
        stray = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(stray)
        self.set_gpus(apps=[(1, stray.pid)])
        self.bus("release", d["id"], check=True)
        self.wait_state(d["id"], "needs-reconcile", 40 * SCALE)
        os.kill(self.state(d["id"])["supervisor"]["pid"], signal.SIGSTOP)   # hold the supervisor still
        self.bus("reconcile", d["id"], "--confirm", str(stray.pid), check=True)
        ctl = next((self.home / "state" / "ctl" / d["id"]).glob("reconcile-*.json"))
        rec = json.loads(ctl.read_text())["identities"][0]
        self.assertEqual((rec["pid"], rec["ticks"]), (stray.pid, core.proc_ticks(stray.pid)))
        p = self.home / "state" / "runs" / f"{d['id']}.json"
        s = json.loads(p.read_text())
        s["reconciled"] = [dict(rec, ticks=rec["ticks"] - 1)]   # as if confirmed for an earlier process of that pid
        p.write_text(json.dumps(s))
        ctl.unlink()
        os.kill(self.state(d["id"])["supervisor"]["pid"], signal.SIGCONT)
        time.sleep(3)
        self.assertEqual(self.state(d["id"])["state"], "needs-reconcile")    # the old clearance does not apply

    def test_a_held_process_that_left_every_gpu_can_be_confirmed(self):
        d = self.start(SLEEP, "1")
        stray = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(stray)
        self.set_gpus(apps=[(1, stray.pid)])
        self.bus("release", d["id"], check=True)
        self.wait_state(d["id"], "needs-reconcile", 40 * SCALE)
        self.set_gpus()                                    # off every GPU, still alive
        time.sleep(2)
        self.assertEqual(self.state(d["id"])["state"], "needs-reconcile")
        r = self.bus_json("reconcile", d["id"])
        self.assertIn(stray.pid, r["candidates"])
        self.bus("reconcile", d["id"], "--confirm", str(stray.pid), check=True)
        self.wait_state(d["id"], "ended", 20)
        self.assertIsNone(stray.poll())


# ------------------------------------------------------------------------------------------------ item 4

@need_systemd
class ExclusivityAndFallbackGroups(BusTest):
    def test_fallback_never_signals_a_group_whose_leader_is_not_the_recorded_one(self):
        other = subprocess.Popen(["sleep", "300"], start_new_session=True)   # an unrelated group
        self.procs.append(other)
        sup = runner.Supervisor(f"{core.USER}-20261124-000000-fb")
        ident = core.proc_identity(other.pid)
        sup.s = dict(mode="fallback", leader=dict(ident, ticks=ident["ticks"] + 1))   # a different, earlier process
        sup.child = (other.pid, other.pid, None)
        sup.stop_signal("TERM")
        time.sleep(0.3)
        self.assertIsNone(other.poll(), "a reused group id was signalled")
        sup.s["leader"] = ident
        sup.stop_signal("TERM")
        self.assertEqual(other.wait(10), -signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
