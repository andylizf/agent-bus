"""Regression tests for the ninth review round (bus side): each reproduces the scenario the review described."""
import json, os, subprocess, time, unittest

from helpers import LINUX, BusTest, need_systemd, wait_for
from agentbus import core, runner


class AncestryLive(BusTest):
    @unittest.skipUnless(LINUX, "pidfd")
    def test_a_parent_now_elsewhere_does_not_vouch_from_a_cached_cgroup(self):
        parent = subprocess.Popen(["bash", "-c", "sleep 300 & echo $! > " + str(self.tmp / "c.pid") + "; wait"],
                                  start_new_session=True)
        self.procs.append(parent)
        child = wait_for(lambda: int((self.tmp / "c.pid").read_text()) if (self.tmp / "c.pid").exists() and
                         (self.tmp / "c.pid").read_text().strip() else None, 10, what="the child")
        stale = {parent.pid: "/user.slice/the.slice/old"}     # what an earlier scan saw: the parent in the slice
        self.assertFalse(runner._reaches(child, "/user.slice/the.slice", {}, stale))
        here = core.proc_cgroup(parent.pid)                  # where the parent really is, read live
        self.assertTrue(runner._reaches(child, here, {}, {}))


class Generations(BusTest):
    def sup(self, rid, run):
        s = runner.Supervisor(rid)
        s.s = dict(id=rid, host="h", gpus=[0], run=run, started=1, until=2, kind="other", entry_state="ended",
                   end_reason="exited rc=0", **{"class": "backfill"})
        return s

    def test_an_old_supervisor_never_archives_a_newer_generation(self):
        rid = f"{core.USER}-20261125-000000-gen"
        (self.root / "agents" / f"{rid}.json").write_text(json.dumps(dict(id=rid, run="b" * 32, state="running")))
        with core.Board(str(self.root)) as b:
            old = self.sup(rid, "a" * 32)
            old.archive_entry(b)
        self.assertTrue((self.root / "agents" / f"{rid}.json").exists())
        self.assertEqual(json.loads((self.root / "agents" / f"{rid}.json").read_text())["run"], "b" * 32)
        self.assertIn("generation", old.s["archive_error"])

    def test_an_old_supervisor_never_overwrites_a_newer_generation(self):
        rid = f"{core.USER}-20261125-000001-gen"
        (self.root / "agents" / f"{rid}.json").write_text(json.dumps(dict(id=rid, run="b" * 32, state="running")))
        old = self.sup(rid, "a" * 32)
        old.s["entry_state"] = "running"
        with core.Board(str(self.root)) as b:
            with self.assertRaises(core.Refused):
                old.write_entry(b)
        self.assertEqual(json.loads((self.root / "agents" / f"{rid}.json").read_text())["run"], "b" * 32)


@need_systemd
class ReleaseByGeneration(BusTest):
    def test_release_naming_another_generation_is_refused(self):
        d = self.start("sleep 300")
        r = self.bus("release", d["id"], "--run", "0" * 32, "--json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("another generation", r.stderr)
        self.assertEqual(self.state(d["id"])["state"], "running")
        self.assertTrue(self.bus_json("release", d["id"], "--run", d["run"], "--wait", "60", "--json")["released"])


if __name__ == "__main__":
    unittest.main()
