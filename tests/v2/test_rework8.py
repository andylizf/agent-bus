"""Regression tests for the eighth review round (bus side): each reproduces the scenario the review described."""
import json, os, subprocess, time, unittest
from unittest import mock

from helpers import LINUX, BusTest, wait_for
from agentbus import core, runner


class AncestryByIdentity(BusTest):
    @unittest.skipUnless(LINUX, "/proc")
    def test_a_recycled_ancestor_pid_vouches_for_nothing(self):
        parent = subprocess.Popen(["bash", "-c", "sleep 300 & echo $! > " + str(self.tmp / "c.pid") + "; wait"],
                                  start_new_session=True)
        self.procs.append(parent)
        child = wait_for(lambda: int((self.tmp / "c.pid").read_text()) if (self.tmp / "c.pid").exists() and
                         (self.tmp / "c.pid").read_text().strip() else None, 10, what="the child")
        real = core.proc_identity(parent.pid)
        recycled = {parent.pid: dict(real, ticks=real["ticks"] - 1)}   # a record of an earlier process at that pid
        self.assertFalse(runner._reaches(child, "/no.slice", recycled, {}))
        self.assertTrue(runner._reaches(child, "/no.slice", {parent.pid: real}, {}))
        self.assertFalse(runner._reaches(child, "/no.slice", {}, {}))     # a pruned ancestor vouches for nothing


class GcGenerationAndBoard(BusTest):
    def entry(self, rid, run):
        e = dict(id=rid, user=core.USER, host="h", gpus=[0], booking=None, run=run, started=1, until=2,
                 kind="other", exclusive=False, note="", state="running", schema=2)
        e["class"] = "backfill"
        (self.root / "agents" / f"{rid}.json").write_text(json.dumps(e))

    def test_gc_archives_only_the_same_generation_on_the_same_board(self):
        with core.Board(str(self.root)) as b:
            pin = dict(path=b.path, inode=b.ino, fsid=b.fsid)
        other_gen, other_board, same = (f"{core.USER}-20261125-00000{i}-g" for i in range(3))
        for rid in (other_gen, other_board, same):
            self.entry(rid, "b" * 32)
        core.write_private(runner.state_path(other_gen), dict(id=other_gen, state="ended", run="c" * 32, board=pin))
        core.write_private(runner.state_path(other_board), dict(id=other_board, state="ended", run="b" * 32,
                                                                board=dict(pin, inode=pin["inode"] + 1)))
        core.write_private(runner.state_path(same), dict(id=same, state="ended", run="b" * 32, board=pin))
        r = self.bus_json("gc", "--mine", "--json")
        self.assertEqual(r["archived"], [same])
        self.assertEqual(sorted(r["unknown"]), sorted([other_gen, other_board]))
        self.assertTrue((self.root / "agents" / f"{other_gen}.json").exists())
        self.assertTrue((self.root / "agents" / f"{other_board}.json").exists())


class PidfdSurfaced(BusTest):
    def test_doctor_reports_pidfd(self):
        d = json.loads(self.bus("doctor", "--json").stdout)
        c = next(c for c in d["checks"] if c["check"] == "pidfd")
        self.assertEqual(c["ok"], hasattr(os, "pidfd_open"))

    def test_owner_told_once_when_pidfd_is_missing(self):
        sup = runner.Supervisor(f"{core.USER}-20261125-000000-p")
        sup.s = dict(mode="systemd", notices=[], run="a" * 32)
        sup.hooks = runner.Hooks(None)
        with mock.patch.object(runner, "pidfd_available", return_value=False):
            sup.check_pidfd()
            sup.check_pidfd()
        self.assertEqual([n["event"] for n in sup.s["notices"]], ["pidfd-unavailable"])


if __name__ == "__main__":
    unittest.main()
