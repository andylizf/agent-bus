"""Regression tests for the seventh review round (bus side): each reproduces the scenario the review described."""
import json, os, signal, subprocess, time, unittest
from unittest import mock

from helpers import LINUX, SCALE, BusTest, need_systemd, wait_for
from agentbus import core, runner

SLEEP = "sleep 300"


@need_systemd
class PartialReleaseRecomputed(BusTest):
    def test_a_populated_slice_clears_an_earlier_partial_release(self):
        d = self.start(f"echo $$ > {self.tmp}/j.pid; exec sleep 300", "0-1")
        jpid = wait_for(lambda: int((self.tmp / "j.pid").read_text()) if (self.tmp / "j.pid").exists() else None, 10,
                        what="job pid")
        self.set_gpus(apps=[(1, jpid)])                       # the job's own process on GPU 1, slice populated
        sup = runner.Supervisor(d["id"])
        sup.s = dict(self.state(d["id"]), held_gpus=[0], gpus_released=[1], state="needs-reconcile")
        ok, remains = sup.verify()
        self.assertFalse(ok)
        self.assertEqual((sup.s["held_gpus"], sup.s["gpus_released"]), ([0, 1], []))
        self.assertEqual(sup.entry()["gpus"], [0, 1])


class EscapeIdentity(BusTest):
    @unittest.skipUnless(LINUX, "pidfd")
    def test_a_process_that_exits_during_the_check_is_not_recorded(self):
        sup = runner.Supervisor(f"{core.USER}-20261124-000000-e")
        child = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.procs.append(child)
        sup.s = dict(mode="systemd", units=dict(cgroup_path="/nonexistent.slice"), escaped={})
        with mock.patch.object(runner, "descendants_outside", return_value={child.pid: "/user.slice/x.scope"}), \
                mock.patch.object(runner, "_reaches", return_value=True), \
                mock.patch.object(runner.signal, "pidfd_send_signal", side_effect=ProcessLookupError()):
            rec = sup.track_escaped()
        self.assertEqual(rec, {})
        with mock.patch.object(runner, "descendants_outside", return_value={child.pid: "/user.slice/x.scope"}), \
                mock.patch.object(runner, "_reaches", return_value=True):
            rec = sup.track_escaped()
        self.assertEqual(rec[str(child.pid)]["ticks"], core.proc_ticks(child.pid))

    @unittest.skipUnless(LINUX, "/proc")
    def test_a_stale_fallback_run_does_not_claim_a_recycled_group(self):
        other = subprocess.Popen(["sleep", "300"], start_new_session=True)     # some job's worker, group = its pid
        self.procs.append(other)
        ident = core.proc_identity(other.pid)
        stale = dict(state_version=1, id=f"{core.USER}-20261124-000000-old", state="running", mode="fallback",
                     host=core.HOST, pgid=other.pid, leader=dict(ident, ticks=ident["ticks"] - 1),
                     units={}, gpus=[0])
        core.write_private(runner.state_path(stale["id"]), stale)
        attr = runner.other_runs_attribution(f"{core.USER}-20261124-000000-me")
        self.assertIsNone(attr(other.pid))
        stale["leader"] = ident                                   # its real, live leader: attributed
        core.write_private(runner.state_path(stale["id"]), stale)
        self.assertEqual(runner.other_runs_attribution("x")(other.pid), stale["id"])


class GcUnderTheRunLock(BusTest):
    def entry(self, rid):
        e = dict(id=rid, user=core.USER, host="h", gpus=[0], booking=None, run="a" * 32, started=1, until=2,
                 kind="other", exclusive=False, note="", state="running", schema=2)
        e["class"] = "backfill"
        (self.root / "agents" / f"{rid}.json").write_text(json.dumps(e))

    def test_gc_archives_only_runs_positively_ended(self):
        live, ended, unknown = (f"{core.USER}-20261124-00000{i}-g" for i in range(3))
        for rid in (live, ended, unknown):
            self.entry(rid)
        with core.Board(str(self.root)) as b:
            pin = dict(path=b.path, inode=b.ino, fsid=b.fsid)
        core.write_private(runner.state_path(live), dict(id=live, state="running", host="elsewhere", run="a" * 32,
                                                         board=pin))
        core.write_private(runner.state_path(ended), dict(id=ended, state="ended", host=core.HOST, run="a" * 32,
                                                          board=pin))
        r = self.bus_json("gc", "--mine", "--json")
        self.assertEqual(r["archived"], [ended])
        self.assertEqual(r["unknown"], [unknown])
        self.assertTrue((self.root / "agents" / f"{live}.json").exists())

    def test_gc_skips_an_entry_whose_run_lock_is_held(self):
        rid = f"{core.USER}-20261124-000009-held"
        self.entry(rid)
        core.write_private(runner.state_path(rid), dict(id=rid, state="ended", host=core.HOST))
        lk = runner.Locked(os.path.join(runner.runs_dir(), f"{rid}.lock"))
        self.assertTrue(lk.acquire())
        try:
            r = self.bus_json("gc", "--mine", "--json")
        finally:
            lk.release()
        self.assertEqual(r["archived"], [])
        self.assertTrue((self.root / "agents" / f"{rid}.json").exists())


class ArchiveWithoutTheFlag(BusTest):
    def test_an_entry_written_before_the_state_was_saved_is_still_archived(self):
        """The first supervisor wrote its entry and was SIGKILLed before saving entry_on_board; the adopter ends the
        run and must archive the entry that is on the board."""
        rid = f"{core.USER}-20261125-000000-flag"
        sup = runner.Supervisor(rid)
        sup.s = dict(id=rid, host="h", gpus=[0], run="a" * 32, started=1, until=2, kind="other", entry_state="ended",
                     end_reason="exited rc=0", **{"class": "backfill"})          # no entry_on_board
        (self.root / "agents" / f"{rid}.json").write_text(json.dumps(dict(id=rid, run="a" * 32)))
        with core.Board(str(self.root)) as b:
            sup.archive_entry(b)
        self.assertFalse((self.root / "agents" / f"{rid}.json").exists())
        self.assertTrue(list((self.root / "archive").glob(f"{rid}-*.json")))


class ArchiveRewrite(BusTest):
    def test_a_failed_end_record_rewrite_is_recorded(self):
        rid = f"{core.USER}-20261124-000000-arc"
        sup = runner.Supervisor(rid)
        sup.s = dict(id=rid, host="h", gpus=[0], run="a" * 32, started=1, until=2, kind="other", entry_state="ended",
                     end_reason="exited rc=0", entry_on_board=True, **{"class": "backfill"})
        (self.root / "agents" / f"{rid}.json").write_text(json.dumps(dict(id=rid, run="a" * 32)))
        with core.Board(str(self.root)) as b, mock.patch.object(core, "replace", side_effect=OSError(5, "EIO")):
            sup.archive_entry(b)
        self.assertIn("rewrite", sup.s.get("archive_error", ""))
        self.assertFalse((self.root / "agents" / f"{rid}.json").exists())


if __name__ == "__main__":
    unittest.main()
