import io
import json
import os
import subprocess
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from helpers import BusTest, PY, REPO
from agentbus import bookings, core, move, runner, setup


class BoardMove(BusTest):
    def setUp(self):
        super().setUp()
        self.pin = core.load_conf()
        self.retired = self.root.with_name(self.root.name + ".retired-" + self.pin["inode"])
        self.target = self.tmp / "new"
        setup.setup_board(str(self.target), host=self.host(), blocks=self.blocks)
        (self.target / "config").write_bytes((self.root / "config").read_bytes())

    def move(self, action, check=True):
        return self.bus("move-board", action, str(self.root), str(self.target), check=check)

    def activate(self):
        self.move("stage")
        self.move("finish")

    def supervisor(self):
        rid = core.new_id("move")
        sup = runner.Supervisor(rid)
        sup.hooks = Mock()
        sup.leader_exit_step = Mock()
        sup.sample = Mock()
        t = core.now()
        sup.s = dict(id=rid, board={k: self.pin[k] for k in ("path", "inode", "fsid")},
                     state="running", entry_state="running",
                     mode="fallback", host=core.HOST, gpus=[0], run="a" * 32, started=t, until=t + 3600,
                     kind="other", requests={}, **{"class": "backfill"})
        return sup

    def test_owner_move_repins_install_and_keeps_archive(self):
        self.activate()
        self.assertFalse(self.root.exists())
        self.assertEqual(str(self.retired.stat().st_ino), self.pin["inode"])
        result = self.bus_json("ls", "--json")
        self.assertEqual(result["board"], dict(path=str(self.target), identity="ok"))
        conf = core.load_conf()
        self.assertEqual(conf["path"], str(self.target))
        self.assertEqual(conf["inode"], str(self.target.stat().st_ino))
        self.assertEqual(conf["fsid"], str(os.statvfs(self.target).f_fsid))

    def test_wrong_record_owner_and_writable_record_are_refused(self):
        self.activate()
        read = core.read_json

        def changed_owner(fd, name, *args):
            result = read(fd, name, *args)
            if name == move.RECORD and result:
                obj, st, sha = result
                return obj, SimpleNamespace(st_uid=st.st_uid + 1, st_mode=st.st_mode), sha
            return result

        with patch.object(core, "read_json", side_effect=changed_owner):
            with self.assertRaises(core.Refused):
                core.Board.from_conf()
        (self.retired / move.RECORD).chmod(0o666)
        self.assertNotEqual(self.bus("ls").returncode, 0)
        self.assertEqual(core.load_conf(), self.pin)

    def test_target_root_owned_by_another_user_is_refused(self):
        self.activate()
        fstat = os.fstat
        ino = self.target.stat().st_ino

        def changed_owner(fd):
            st = fstat(fd)
            if st.st_ino == ino:
                return SimpleNamespace(st_mode=st.st_mode, st_ino=ino, st_uid=st.st_uid + 1)
            return st

        with patch.object(core.os, "fstat", side_effect=changed_owner):
            with self.assertRaises(core.Refused):
                core.Board.from_conf()
        self.assertEqual(core.load_conf(), self.pin)

    def test_path_symlinks_are_refused(self):
        self.activate()
        real = self.tmp / "elsewhere"
        self.target.rename(real)
        self.target.symlink_to(real, target_is_directory=True)
        self.assertNotEqual(self.bus("ls").returncode, 0)
        self.target.unlink()
        real.rename(self.target)
        link = self.tmp / "linked-parent"
        link.symlink_to(self.tmp, target_is_directory=True)
        record = self.retired / move.RECORD
        original = record.read_bytes()
        record.write_bytes(core.dumps(dict(json.loads(original), path=str(link / self.target.name))))
        self.assertNotEqual(self.bus("ls").returncode, 0)
        record.write_bytes(original)
        held = self.retired / "held.json"
        record.rename(held)
        record.symlink_to(held)
        self.assertNotEqual(self.bus("ls").returncode, 0)
        self.assertEqual(core.load_conf(), self.pin)

    def test_record_must_match_host_and_target_identity(self):
        self.activate()
        record = self.retired / move.RECORD
        data = json.loads(record.read_bytes())
        for change in (dict(host="another-host"), dict(inode=data["inode"] + 1), dict(fsid=data["fsid"] + 1)):
            with self.subTest(change=change):
                record.write_bytes(core.dumps(dict(data, **change)))
                self.assertNotEqual(self.bus("ls").returncode, 0)
                self.assertEqual(core.load_conf(), self.pin)

    def test_rollback_before_finish_leaves_both_pins_on_source(self):
        sup = self.supervisor()
        sup.one_pass()
        self.move("stage")
        sup.one_pass()
        self.retired.rename(self.root)
        sup.one_pass()
        self.assertEqual(core.load_conf(), self.pin)
        self.assertEqual(sup.s["board"]["path"], str(self.root))
        self.assertEqual(sup.s["_entry_board"]["path"], str(self.root))
        self.assertEqual(self.bus_json("ls", "--json")["board"]["path"], str(self.root))

    def test_staged_supervisor_replays_and_repins_between_passes(self):
        sup = self.supervisor()
        sup.one_pass()
        name = f"{sup.rid}.json"
        self.assertTrue((self.root / "agents" / name).exists())
        t = core.now()
        with core.Board.from_conf() as board:
            info = bookings.publish_booking(board, [1], t + 60, t + 120, objection_s=2)
        self.move("stage")
        self.assertIn(core.USER, self.move("finish", check=False).stderr)
        sup.one_pass()
        self.assertEqual(sup.s["board"]["path"], str(self.root))
        self.assertEqual(core.load_conf(), self.pin)
        self.assertTrue((self.target / "agents" / name).exists())
        self.assertTrue((self.target / "agents" / ".heartbeat" / sup.rid).exists())
        self.assertEqual((self.target / "inbox" / sup.rid).stat().st_mode & 0o7777, 0o1777)
        self.assertTrue((self.target / "bookings" / (info["id"] + ".json")).exists())
        self.assertEqual(sup.leader_exit_step.call_count, 2)
        self.assertNotEqual(self.bus("mailbox", "writes-paused").returncode, 0)
        self.move("replay")
        self.move("finish")
        sup.one_pass()
        self.assertEqual(sup.s["board"]["path"], str(self.target))
        self.assertEqual(core.load_conf()["path"], str(self.target))
        with core.Board.from_conf() as board:
            row = bookings.evaluate(board)[0]
        self.assertEqual((row["slot"], row["revision"], row["objection_close"]),
                         (info["slot"], info["revision"], info["objection_close"]))
        self.assertEqual(sup.s["run"], "a" * 32)
        self.assertEqual(sup.s["state"], "running")

    def test_v208_pass_keeps_running_without_board_writes(self):
        archive = subprocess.run(["git", "archive", "63a8926", "lib"], cwd=REPO, capture_output=True)
        if archive.returncode:
            self.skipTest("the v2.0.8 git object is unavailable")
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tree:
            tree.extractall(self.tmp)
        sup = self.supervisor()
        sup.one_pass()
        self.move("stage")
        heartbeat = self.retired / "agents" / ".heartbeat" / sup.rid
        mtime = heartbeat.stat().st_mtime_ns
        script = """
import sys
from unittest.mock import Mock
sys.path.insert(0, sys.argv[1])
from agentbus import runner
sup = runner.Supervisor(sys.argv[2])
sup.s = runner.load_state(sup.rid)
sup.hooks = Mock()
sup.sample = Mock()
sup.leader_exit_step = Mock()
sup.one_pass()
assert sup.s['state'] == 'running'
assert sup.s['identity_alarm']
assert not sup.leader_exit_step.call_args[0][0].ok
"""
        proc = subprocess.run([PY, "-IB", "-c", script, str(self.tmp / "lib"), sup.rid],
                              env=self.env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(heartbeat.stat().st_mtime_ns, mtime)
        self.assertFalse((self.target / "agents" / (sup.rid + ".json")).exists())
