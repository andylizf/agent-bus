"""Regression tests for the sixth review round (bus side): each reproduces the scenario the review described."""
import json, os, signal, subprocess, time, unittest
from unittest import mock

from helpers import LINUX, SCALE, BusTest, need_systemd, wait_for
from agentbus import core, runner

SLEEP = "sleep 300"


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@need_systemd
class FailedScanPublishesAllGpus(BusTest):
    def test_after_a_failed_scan_the_entry_claims_every_original_gpu(self):
        d = self.start(SLEEP, "0-1")
        stray = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(stray)
        self.set_gpus(apps=[(0, stray.pid)])
        self.bus("release", d["id"], check=True)
        self.wait_state(d["id"], "needs-reconcile", 40 * SCALE)
        wait_for(lambda: json.loads(self.entry_file(d["id"]).read_text())["gpus"] == [0], 10, what="narrowed to 0")
        self.set_gpus(fail=True)
        e = wait_for(lambda: (lambda x: x if x["gpus"] == [0, 1] else None)(
            json.loads(self.entry_file(d["id"]).read_text())), 20, what="all GPUs claimed again")
        self.assertEqual(e.get("gpu_view"), "unknown")
        self.set_gpus(apps=[(0, stray.pid)])
        wait_for(lambda: json.loads(self.entry_file(d["id"]).read_text())["gpus"] == [0], 20, what="narrowed again")
        stray.kill()
        stray.wait()
        self.wait_state(d["id"], "ended", 20)


@need_systemd
class FailedLaunch(BusTest):
    def test_a_launch_systemd_never_started_closes_admission_and_is_verified(self):
        fake = self.tmp / "bin"
        fake.mkdir()
        (fake / "systemd-run").write_text('#!/bin/bash\nfor a in "$@"; do case $a in *-main.service*) '
                                          'echo "Failed to start transient service unit" >&2; exit 1;; esac; done\n'
                                          'exec /usr/bin/systemd-run "$@"\n')
        (fake / "systemd-run").chmod(0o755)
        env = dict(self.env, PATH=f"{fake}:{os.environ['PATH']}")
        info = self.tmp / "i.json"
        r = self.bus("run", "--gpus", "0", "--info-file", str(info), "--", SLEEP, env=env, timeout=120)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("did not start", r.stderr)
        rid = json.loads(info.read_text())["id"]
        st = self.state(rid)
        self.assertEqual(st["state"], "ended")
        self.assertEqual(runner.admit_read(rid)["admission"], "closed")
        self.assertTrue(st.get("release"), "ended through the verified release")
        self.assertIn("released", [n["event"] for n in st["notices"]])
        self.assertIn("closed", self.bus("exec", rid, "--run", st["run"], "--", "true").stderr)


@need_systemd
class ProcessJoiningAnEscapedUnit(BusTest):
    def test_a_process_joining_after_enumeration_is_never_signalled(self):
        pidf = self.tmp / "w.pid"
        unit = f"{self.prefix}-esc"
        d = self.start(f"systemd-run --user --quiet --scope --unit={unit} -- bash -c 'echo $$ > {pidf}; exec sleep 300' "
                       f"& sleep 300")
        worker = wait_for(lambda: int(pidf.read_text()) if pidf.exists() and pidf.read_text().strip() else None, 20,
                          what="the worker")
        wait_for(lambda: str(worker) in (self.state(d["id"]).get("escaped") or {}), 10, what="recorded")
        stall = self.tmp / "stall"
        stall.touch()                    # hold the supervisor between passes
        env = dict(self.env, AGENT_BUS_TEST_STALL=str(stall))
        cg = core.proc_cgroup(worker)
        late = subprocess.Popen(["systemd-run", "--user", "--quiet", "--scope", f"--unit={self.prefix}-late", "--",
                                 "sleep", "300"], start_new_session=True)
        self.procs.append(late)
        wait_for(lambda: f"{self.prefix}-late" in (core.proc_cgroup(late.pid) or ""), 10, what="late scope")
        with open(f"/sys/fs/cgroup{cg}/cgroup.procs", "w") as f:     # joins the worker's unit after enumeration
            f.write(str(late.pid))
        stall.unlink()
        r = self.bus_json("release", d["id"], "--wait", str(40 * SCALE), "--json")
        self.assertTrue(r["released"], r)
        self.assertFalse(alive(worker))
        self.assertIsNone(late.poll(), "a process that only shares the unit was signalled")


class IdentitiesEndToEnd(BusTest):
    @unittest.skipUnless(LINUX, "/proc")
    def test_a_pid_reused_during_the_query_is_not_cleared(self):
        p = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(p)
        ident = core.proc_identity(p.pid)
        old = dict(ident, ticks=ident["ticks"] - 1)          # confirmed for an earlier process with this pid
        self.set_gpus(apps=[(0, p.pid)])
        clear, mine, other, err = runner.gpu_release_check("nobody-x", [0], [old])
        self.assertEqual(mine, [p.pid])
        clear, mine, other, err = runner.gpu_release_check("nobody-x", [0], [ident])
        self.assertEqual(mine, [])

    @unittest.skipUnless(LINUX, "/proc")
    def test_an_ownership_lookup_error_is_held_not_taken_for_an_exit(self):
        p = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(p)
        self.set_gpus(apps=[(0, p.pid)])
        with mock.patch.object(core, "proc_owner", return_value=(None, "error")):
            clear, mine, other, err = runner.gpu_release_check("nobody-x", [0], [])
        self.assertFalse(clear)
        self.assertEqual(mine, [p.pid])

    @unittest.skipUnless(LINUX, "/proc")
    def test_identity_state_is_three_valued(self):
        p = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(p)
        ident = core.proc_identity(p.pid)
        self.assertEqual(core.identity_state(ident), "alive")
        self.assertEqual(core.identity_state(dict(ident, ticks=None)), "unknown")
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            self.assertEqual(core.identity_state(ident), "unknown")
        p.kill()
        p.wait()
        self.assertEqual(core.identity_state(ident), "gone")


@need_systemd
class UnidentifiableLauncher(BusTest):
    def test_an_exec_whose_launcher_cannot_be_identified_is_cleared_by_the_owner(self):
        d = self.start(SLEEP)
        env = dict(self.env, AGENT_BUS_TEST_LAUNCHER_IDENT_FAIL="1", AGENT_BUS_TEST_EXEC_PAUSE="launcher")
        client = self.bg("exec", d["id"], "--run", d["run"], "--", "sleep", "300", env=env)
        it = wait_for(lambda: next((x for x in runner.admit_read(d["id"])["inflight"] if x.get("launcher")), None),
                      15, what="the launcher")
        self.assertIsNone(it["launcher"]["ticks"])
        os.kill(client.pid, signal.SIGKILL)
        client.wait()
        os.kill(it["launcher"]["pid"], signal.SIGKILL)       # the launcher is gone too, but nobody can tell
        self.bus("release", d["id"], check=True)
        st = wait_for(lambda: (lambda x: x if "exec-unidentified" in [n["event"] for n in x["notices"]] else None)(
            self.state(d["id"])), 20, what="the owner told")
        self.assertEqual(st["state"], "stopping")
        r = self.bus_json("reconcile", d["id"], "--cancel-exec", it["exec_id"])
        self.assertEqual(r["cancelled"], it["exec_id"])
        self.wait_state(d["id"], "ended", 40 * SCALE)


if __name__ == "__main__":
    unittest.main()
