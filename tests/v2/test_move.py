import io
import json
import os
import subprocess
import tarfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from helpers import BusTest, PY, REPO, wait_for
from agentbus import auto_upgrade, bookings, cli, core, move, runner, setup


class BoardMove(BusTest):
    def setUp(self):
        super().setUp()
        self.pin = core.load_conf()
        self.retired = self.root.with_name(self.root.name + ".retired-" + self.pin["inode"])
        self.addCleanup(self.restore_test_modes)
        self.target = self.tmp / "new"
        setup.setup_board(str(self.target), host=self.host(), blocks=self.blocks)
        (self.target / "config").write_bytes((self.root / "config").read_bytes())

    def move(self, action, check=True):
        return self.bus("move-board", action, str(self.root), str(self.target), "--wait", "0", check=check)

    def restore_test_modes(self):
        if self.retired.exists():
            with core.Board(str(self.retired)) as old:
                for part, mode in (move.owned_json(old, move.SEAL, core.MOVE_LIMIT) or {}).items():
                    move.directory_mode(old, part.split("/"), mode)

    def activate(self):
        self.move("stage")
        self.move("finish")

    def book(self, board, gpu=0, start=None):
        move.announce(board, runner.build_version())
        start = core.now() + 600 if start is None else start
        return bookings.publish_booking(board, [gpu], start, start + 600, objection_s=1)

    def close_window(self, booking):
        wait_for(lambda: core.now() >= booking["objection_close"], 4, what="objection window closing")

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
                     kind="other", requests={}, supervisor={}, **{"class": "backfill"})
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
        self.assertFalse(self.retired.exists())
        self.target.rename(self.tmp / "unused-staged-board")
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
        self.move("stage")
        sup.one_pass()
        self.assertEqual(sup.s["board"]["path"], str(self.root))
        self.assertEqual(core.load_conf(), self.pin)
        self.assertFalse((self.target / "agents" / name).exists())
        self.bus("mailbox", "between-stage-and-finish", check=True)
        with core.Board.from_conf() as board:
            info = self.book(board, 1)
        result = self.move("finish", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("open objection windows", result.stderr)
        self.close_window(info)
        self.move("finish")
        sup.one_pass()
        self.assertTrue((self.target / "agents" / name).exists())
        self.assertTrue((self.target / "agents" / ".heartbeat" / sup.rid).exists())
        self.assertEqual((self.target / "inbox" / sup.rid).stat().st_mode & 0o7777, 0o1777)
        self.assertTrue((self.target / "bookings" / (info["id"] + ".json")).exists())
        self.assertEqual(sup.leader_exit_step.call_count, 3)
        self.assertTrue((self.target / "inbox" / f"{core.USER}-between-stage-and-finish").is_dir())
        self.assertEqual(sup.s["board"]["path"], str(self.target))
        self.assertEqual(core.load_conf()["path"], str(self.target))
        with core.Board.from_conf() as board:
            row = bookings.evaluate(board)[0]
        self.assertEqual((row["slot"], row["revision"]), (info["slot"], info["revision"]))
        self.assertGreaterEqual(row["objection_close"], info["objection_close"])
        self.assertEqual(sup.s["run"], "a" * 32)
        self.assertEqual(sup.s["state"], "running")

    def test_finish_drains_aware_writer_and_seals_legacy_descriptor(self):
        box = self.bus("mailbox", "seal", check=True).stdout.strip()
        self.move("stage")
        afd = core.walk(str(self.root / "arrivals"))
        mailbox = core.walk(str(self.root / "inbox" / box))
        try:
            with core.Board.from_conf() as board:
                command = [PY, "-IB", self.entry_point(), "move-board", "finish",
                           str(self.root), str(self.target), "--wait", "0"]
                p = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                info = self.book(board, 1)
                self.close_window(info)
                self.assertTrue(self.root.exists())
            stdout, stderr = p.communicate(timeout=10)
            self.assertEqual(p.returncode, 0, (stdout, stderr))
            self.assertFalse(self.root.exists())
            with self.assertRaises(PermissionError):
                core.publish(afd, "legacy-arrival.json", b"{}")
            with self.assertRaises(PermissionError):
                core.publish(mailbox, "legacy-mail.json", b"{}")
            self.assertEqual(self.bus_json("ls", "--json")["bookings"][0]["id"], info["id"])
        finally:
            os.close(afd)
            os.close(mailbox)

    def test_two_same_user_clients_replay_once_and_reserve_original_slots(self):
        t = core.now()
        with core.Board.from_conf() as board:
            info = self.book(board)
        self.close_window(info)
        self.activate()
        command = [PY, "-IB", self.entry_point(), "ls", "--json"]
        clients = [subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for client in clients:
            raw, err = client.communicate(timeout=10)
            self.assertEqual(client.returncode, 0, err)
            self.assertEqual(json.loads(raw)["bookings"][0]["id"], info["id"])
        with core.Board.from_conf() as board:
            new = bookings.publish_booking(board, [1], t + 120, t + 600, objection_s=60)
            self.assertGreater(new["slot"], info["slot"])
            self.assertEqual(list(board.receipts()), [info["slot"], new["slot"]])

    def test_messages_published_during_stage_follow_their_sender(self):
        box = f"{core.USER}-test"
        self.bus("mailbox", "test", check=True)
        self.move("stage")
        with core.Board.from_conf() as board:
            valid = dict(id=core.new_id("during-stage"), sender=core.USER, to=box, kind="notice", note="local fixture",
                         schema=core.SCHEMA, created=core.now())
            invalid = dict(valid, id=core.new_id("advisory"))
            board.send(box, valid)
            core.publish(board.wdir("inbox", box), invalid["id"] + ".json", core.dumps(invalid), 0o666)
        self.move("finish")
        with core.Board.from_conf() as board:
            self.assertEqual([r["id"] for r in board.messages(box)], [valid["id"]])

    def test_stale_old_run_still_holds_exclusive_until_its_exit_is_confirmed(self):
        sup = self.supervisor()
        sup.one_pass()
        heartbeat = self.root / "agents" / ".heartbeat" / sup.rid
        os.utime(heartbeat, (time.time() - 3600,) * 2)
        self.move("stage")
        result = self.move("finish")
        self.assertIn(sup.rid, result.stdout)
        with core.Board(str(self.target)) as board, patch.object(
                runner, "nvidia_view", return_value=(True, {0: "fake"}, [], {})):
            self.assertIn(sup.rid, runner.handoff_waits(board, [], dict(id="another", gpus=[0]))[0])
        sup.s["state"] = "ended"
        sup.save()
        result = self.bus_json("ls", "--json")
        self.assertEqual((result["entries"], result["old_runs"]), ([], []))

    def test_bad_inbox_does_not_interrupt_private_cleanup(self):
        sup = self.supervisor()
        sup.one_pass()
        self.activate()
        (self.target / "inbox" / sup.rid).write_text("occupied")
        sup.one_pass()
        self.assertEqual(sup.leader_exit_step.call_count, 2)
        self.assertEqual(sup.s["board"]["path"], str(self.target))
        self.assertTrue(sup.s["inbox_unavailable"])

    def test_replay_keeps_closed_objection_clock_and_missing_objector_cannot_grant_booking(self):
        t = core.now()
        with core.Board.from_conf() as board:
            old = self.book(board)
            self.assertTrue(bookings.record_arrival(board, old["id"], old["revision"], core.new_id("arrival"), "a" * 32))
        self.assertIn("open objection windows", self.move("stage", check=False).stderr)
        self.close_window(old)
        self.activate()
        with core.Board.from_conf() as board:
            self.assertEqual(board.arrivals()[0]["revision"], old["revision"])
            board.origin["bookings"][0]["objectors"] = ["missing"]
            seen = core.read_private(os.path.join(core.STATE(), "slots.json"))["slots"]
            with patch.object(move, "completed", return_value={core.USER: board.origin["at"] + 20}):
                row = bookings.evaluate(board, t + 90, seen=seen, record=False)[0]
            self.assertEqual(row["objection_close"], old["objection_close"])
            self.assertEqual(row["state"], "pending")
            self.assertIn("waiting for an objection", row["why"])

    def test_recent_foreign_author_requires_owned_build_announcement(self):
        foreign_uid = core.UID + 1
        (self.root / "log" / "author.jsonl").write_text("{}\n")
        hb = self.root / "agents" / ".heartbeat" / (move.CLIENT_PREFIX + "author.json")
        hb.write_bytes(core.dumps(dict(user="author", build="v2.0.10", move_protocol=move.MOVE_PROTOCOL)))
        read = core.read_json
        original_stat = os.stat

        def by_owner(fd, name, *args):
            row = read(fd, name, *args)
            if name == hb.name and row:
                return row[0], SimpleNamespace(st_uid=foreign_uid, st_ctime=row[1].st_ctime, st_mode=row[1].st_mode), row[2]
            return row

        def record_stat(path, *args, **kwargs):
            st = original_stat(path, *args, **kwargs)
            if path == "author.jsonl":
                return SimpleNamespace(st_uid=foreign_uid, st_mode=st.st_mode, st_ctime=st.st_ctime)
            return st

        with core.Board(str(self.root)) as old, patch.object(core.os, "stat", side_effect=record_stat), \
                patch.object(core, "owner_name", side_effect=lambda uid: "author" if uid == foreign_uid else core.USER):
            with self.assertRaisesRegex(core.Refused, r"accounts without a move-capable build: author"):
                move.quiet(old)
            with patch.object(core, "read_json", side_effect=by_owner):
                move.quiet(old)

    def test_new_account_cannot_launch_during_an_unreplayed_foreign_booking(self):
        with core.Board.from_conf() as board:
            info = self.book(board, start=core.now() + 3)
        self.close_window(info)
        with core.Board(str(self.root)) as old, patch.object(core, "USER", "another-user"):
            with self.assertRaisesRegex(core.Refused, "other accounts' bookings within five minutes"):
                move.quiet(old)
        self.activate()
        with core.Board(str(self.target)) as board, patch.object(core, "USER", "another-user"):
            with self.assertRaisesRegex(core.Refused, "standing booking"):
                runner.classify(board, [], [0], None, info["start"] + 5)

    def test_finish_refuses_legacy_booking_revision_without_receipt(self):
        self.move("stage")
        t = core.now()
        with core.Board.from_conf() as board:
            move.announce(board, runner.build_version())
            with patch.object(bookings, "claim_receipt", side_effect=core.Refused("legacy writer interrupted")):
                with self.assertRaisesRegex(core.Refused, "legacy writer interrupted"):
                    bookings.publish_booking(board, [0], t + 600, t + 1200, objection_s=1)
        result = self.move("finish", check=False)
        self.assertIn("booking revision has no completed receipt", result.stderr)
        self.assertTrue(self.root.exists())
        self.assertFalse(self.retired.exists())

    def test_pending_upgrade_leaves_both_pins_on_source_until_confirmed(self):
        sup = self.supervisor()
        sup.one_pass()
        self.activate()
        core.write_private(auto_upgrade.state_file(), {"pending": {"build": "candidate"}})
        sup.one_pass()
        self.assertEqual(core.load_conf(), self.pin)
        self.assertEqual(sup.s["board"]["path"], str(self.root))
        self.assertFalse((self.target / "agents" / f"{sup.rid}.json").exists())
        core.write_private(auto_upgrade.state_file(), {})
        sup.one_pass()
        self.assertEqual(core.load_conf()["path"], str(self.target))
        self.assertEqual(sup.s["board"]["path"], str(self.target))

    def test_exclusive_board_lock_times_out_and_private_cleanup_runs(self):
        sup = self.supervisor()
        with core.Board(str(self.root)).lease(exclusive=True), patch.object(core, "BOARD_LEASE_S", 0.03):
            sup.one_pass()
        sup.leader_exit_step.assert_called_once()
        self.assertFalse(sup.leader_exit_step.call_args.args[0].ok)
        self.assertIn("lock busy", sup.s["notices"][-1]["detail"])

    def test_launch_has_migratable_entry_before_starting_systemd_unit(self):
        self.move("stage")
        args = cli.parser().parse_args(["run", "--gpus", "0", "--detach", "true"])

        def start(rid, state):
            with core.Board(str(self.root)).lease(exclusive=True) as old:
                runs = move.manifest(old, core.now())["runs"]
                self.assertIn(dict(id=rid, user=core.USER, gpus=[0], run=state["run"]), runs)
            return 0, ""

        with patch.object(runner, "user_manager_ok", return_value=True), \
                patch.object(runner, "start_main_unit", side_effect=start) as main, \
                patch.object(runner, "cgroup_of", return_value="/fixture"), \
                patch.object(runner, "systemd_run", return_value=(0, "")):
            self.assertEqual(runner.cmd_run(args), 0)
            main.assert_called_once()

    def test_install_checks_expected_owner_and_local_host(self):
        a = SimpleNamespace(board=str(self.target), board_owner=None, src=str(REPO), print_host=False)
        with patch.object(core, "DEFAULT_BOARD", str(self.target)), patch.object(cli.pwd, "getpwnam",
                return_value=SimpleNamespace(pw_uid=core.UID + 1)):
            with self.assertRaisesRegex(core.Refused, "must have a root and config"):
                cli.cmd_install(a)
        (self.target / "config").write_text("host=another-host\n")
        with self.assertRaisesRegex(core.Refused, "verified board host"):
            cli.cmd_install(a)
        self.assertEqual(core.load_conf(), self.pin)

    def test_v208_pass_keeps_running_without_board_writes(self):
        archive = subprocess.run(["git", "archive", "63a8926", "lib"], cwd=REPO, capture_output=True)
        if archive.returncode:
            self.skipTest("the v2.0.8 git object is unavailable")
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tree:
            tree.extractall(self.tmp)
        sup = self.supervisor()
        sup.one_pass()
        self.activate()
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
