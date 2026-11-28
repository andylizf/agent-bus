import io
import json
import os
import stat
import subprocess
import tarfile
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import BusTest, PY, REPO, wait_for
from agentbus import auto_upgrade, bookings, cli, core, move, runner, setup


class BoardMove(BusTest):
    def setUp(self):
        super().setUp()
        self.pin = core.load_conf()
        self.retired = self.root.with_name(self.root.name + ".retired-" + self.pin["inode"])
        self.target = self.tmp / "new"
        self.addCleanup(self.restore_test_modes)

    def restore_test_modes(self):
        if self.retired.exists():
            with core.Board(str(self.retired)) as old:
                for part, mode in move.owned_json(old, move.SEAL, core.MOVE_LIMIT).items():
                    move.directory_mode(old, part.split("/"), mode)

    def cutover(self, check=True):
        return self.bus("move-board", str(self.root), str(self.target), check=check)

    def installed_code(self):
        build_root = runner.build_root
        installed = build_root(core.BIN())
        return patch.object(runner, "build_root", side_effect=lambda path=None: build_root(path) if path else installed)

    def supervisor(self):
        rid = core.new_id("move")
        sup = runner.Supervisor(rid)
        sup.hooks = Mock()
        sup.leader_exit_step = Mock()
        sup.sample = Mock()
        t = core.now()
        sup.s = dict(id=rid, board={k: self.pin[k] for k in ("path", "inode", "fsid")},
                     state="running", entry_state="running", mode="fallback", host=core.HOST, gpus=[0],
                     run="a" * 32, started=t, until=t + 3600, kind="other", requests={}, supervisor={},
                     **{"class": "backfill"})
        return sup

    def test_empty_move_creates_board_repins_and_keeps_archive(self):
        self.target.mkdir()  # an owner may reserve the empty destination name
        self.bus("mailbox", "history", check=True)
        self.cutover()
        self.assertFalse(self.root.exists())
        self.assertEqual(str(self.retired.stat().st_ino), self.pin["inode"])
        self.assertTrue((self.retired / "inbox" / f"{core.USER}-history").is_dir())
        self.assertEqual(list((self.target / "inbox").iterdir()), [])
        self.assertEqual(stat.S_IMODE((self.target / "inbox").stat().st_mode), 0o1777)
        self.assertEqual((self.target / "config").read_bytes(), (self.retired / "config").read_bytes())
        result = self.bus_json("ls", "--json")
        self.assertEqual(result["board"], dict(path=str(self.target), identity="ok"))
        self.assertEqual(result["entries"], [])
        conf = core.load_conf()
        self.assertEqual(conf["path"], str(self.target))
        self.assertEqual(conf["inode"], str(self.target.stat().st_ino))
        self.assertEqual(conf["fsid"], str(os.statvfs(self.target).f_fsid))
        self.cutover()  # safe to rerun after success

    def test_wrong_record_owner_and_writable_record_are_refused(self):
        self.cutover()
        read = core.read_json

        def changed_owner(fd, name, *args):
            result = read(fd, name, *args)
            if name == move.RECORD and result:
                obj, st, sha = result
                return obj, SimpleNamespace(st_uid=st.st_uid + 1, st_mode=st.st_mode), sha
            return result

        with self.installed_code(), patch.object(core, "read_json", side_effect=changed_owner):
            with self.assertRaises(core.Refused):
                core.Board.from_conf()
        (self.retired / move.RECORD).chmod(0o666)
        self.assertNotEqual(self.bus("ls").returncode, 0)
        self.assertEqual(core.load_conf(), self.pin)

    def test_target_root_owned_by_another_user_is_refused(self):
        self.cutover()
        fstat = os.fstat
        ino = self.target.stat().st_ino

        def changed_owner(fd):
            st = fstat(fd)
            if st.st_ino == ino:
                return SimpleNamespace(st_mode=st.st_mode, st_ino=ino, st_uid=st.st_uid + 1)
            return st

        with self.installed_code(), patch.object(core.os, "fstat", side_effect=changed_owner):
            with self.assertRaises(core.Refused):
                core.Board.from_conf()
        self.assertEqual(core.load_conf(), self.pin)

    def test_move_and_follow_refuse_path_symlinks(self):
        link = self.tmp / "alias"
        link.symlink_to(self.tmp, target_is_directory=True)
        self.assertNotEqual(self.bus("move-board", str(self.root), str(link / "new")).returncode, 0)
        self.assertTrue(self.root.exists())
        self.cutover()
        real = self.tmp / "elsewhere"
        self.target.rename(real)
        self.target.symlink_to(real, target_is_directory=True)
        self.assertNotEqual(self.bus("ls").returncode, 0)
        self.assertEqual(core.load_conf(), self.pin)

    def test_record_must_match_host_and_target_identity(self):
        self.cutover()
        record = self.retired / move.RECORD
        data = json.loads(record.read_bytes())
        for change in (dict(host="another-host"), dict(inode=data["inode"] + 1), dict(fsid=data["fsid"] + 1)):
            with self.subTest(change=change):
                record.write_bytes(core.dumps(dict(data, **change)))
                self.assertNotEqual(self.bus("ls").returncode, 0)
                self.assertEqual(core.load_conf(), self.pin)

    def test_live_or_unknown_run_refuses_move(self):
        sup = self.supervisor()
        with core.Board.from_conf() as board:
            sup.publish_entry(board)
        result = self.cutover(check=False)
        self.assertIn(sup.rid, result.stderr)
        self.assertIn("runs (including unknown occupancy)", result.stderr)
        (self.root / "agents" / ".heartbeat" / sup.rid).unlink()
        self.assertIn(sup.rid, self.cutover(check=False).stderr)
        self.assertFalse(self.target.exists())

    def test_orphan_inbox_requires_its_owners_archived_end_record(self):
        sup = self.supervisor()
        (self.root / "inbox" / sup.rid).mkdir()
        data = sup.entry()
        (self.root / "archive" / f"{sup.rid}-123.json").write_bytes(core.dumps(data))
        self.bus("adopt", "--all", check=True)
        self.assertIn("run inbox without an end record", self.cutover(check=False).stderr)
        self.assertEqual(self.bus_json("gc", "--mine", "--json")["unknown"], [sup.rid])
        sup.s.update(state="ended", entry_state="ended", ended=core.now())
        sup.save()
        self.assertEqual(self.bus_json("gc", "--mine", "--json")["archived"], [sup.rid])
        self.cutover()
        self.assertTrue((self.retired / "inbox" / sup.rid).exists())

    def test_supervisor_archives_a_run_ended_before_its_first_entry(self):
        sup = self.supervisor()
        (self.root / "inbox" / sup.rid).mkdir()
        sup.s.update(state="ended", entry_state="ended", ended=core.now())
        with self.installed_code(), core.Board.from_conf() as b:
            sup.archive_entry(b)
            archived, = move.archive_entries(b)
            self.assertTrue(move.end_record(archived[0]))
        self.cutover()

    def test_gc_archives_interrupted_admission_only_after_age_and_unit_checks(self):
        args = cli.parser().parse_args(["run", "--gpus", "0", "--detach", "true"])
        write = core.write_private

        for before_state in (True, False):
            with self.subTest(before_state=before_state):
                args.slug = "no-state" if before_state else "preparing"

                def interrupt(path, data):
                    if (before_state and str(path).endswith(".job.json")) or data.get("state") == "starting":
                        raise OSError("interrupted launch")
                    return write(path, data)

                with patch.object(core, "write_private", side_effect=interrupt):
                    with self.assertRaisesRegex(OSError, "interrupted launch"):
                        runner.cmd_run(args)
                inbox, = (p for p in (self.root / "inbox").iterdir() if p.name.endswith(args.slug))
                self.assertEqual(stat.S_IMODE(inbox.stat().st_mode), runner.PREPARING_INBOX_MODE if before_state else 0o1777)
                self.assertEqual(runner.admit_read(inbox.name)["admission"], "closed")
                self.assertIn(inbox.name, self.bus_json("gc", "--mine", "--json")["unknown"])
                aged = int(inbox.stat().st_ctime) + move.ABANDONED_INBOX_S + 2
                gc = cli.parser().parse_args(["gc", "--mine", "--json"])
                with patch.object(core, "now", return_value=aged), self.installed_code(), patch.object(cli, "out") as out:
                    with patch.object(runner, "launch_units_absent", return_value=False):
                        cli.cmd_gc(gc)
                    self.assertIn(inbox.name, out.call_args.args[0]["unknown"])
                    with patch.object(runner, "launch_units_absent", return_value=True):
                        cli.cmd_gc(gc)
                    self.assertEqual(out.call_args.args[0]["archived"], [inbox.name])
                archived = next((self.root / "archive").glob(inbox.name + "-*.json"))
                self.assertIn("run never started", json.loads(archived.read_bytes())["end_reason"])
                self.assertEqual(runner.load_state(inbox.name)["state"], "ended")
        self.cutover()

    def test_gc_closes_admission_and_waits_for_exec_client_and_launcher(self):
        sup = self.supervisor()
        rid = sup.rid
        inbox = self.root / "inbox" / rid
        inbox.mkdir()
        with core.Board.from_conf() as b:
            sup.s.update(state="preparing", mode="systemd", user=core.USER, board=b.pin_fields())
        sup.save()
        client, launcher = {"pid": 123}, {"pid": 456}
        intent = dict(exec_id="fixture", state="launching", client=client, launcher=launcher)
        runner.admit_write(rid, dict(run=sup.s["run"], admission="open", inflight=[intent]))
        with patch.object(runner.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(core.Refused, "admission .* is closed"):
                runner.cmd_exec(rid, sup.s["run"], ["true"])
            spawn.assert_not_called()
        gc = cli.parser().parse_args(["gc", "--mine", "--json"])
        aged = int(inbox.stat().st_ctime) + move.ABANDONED_INBOX_S + 2
        with patch.object(core, "now", return_value=aged), self.installed_code(), patch.object(cli, "out") as out, \
                patch.object(runner, "launch_units_absent", return_value=True):
            for live in (client, launcher):
                with patch.object(core, "identity_gone", side_effect=lambda identity: identity != live):
                    cli.cmd_gc(gc)
                self.assertIn("client or launcher is alive", out.call_args.args[0]["blocked"][rid])
                self.assertEqual(runner.admit_read(rid)["admission"], "closed")
                self.assertEqual(runner.load_state(rid)["state"], "preparing")
                self.assertEqual(list((self.root / "archive").iterdir()), [])
            with patch.object(core, "identity_gone", return_value=True), \
                    patch.object(core, "publish", side_effect=OSError("interrupted archive")):
                cli.cmd_gc(gc)
            self.assertEqual(runner.load_state(rid)["state"], "ended")
            self.assertEqual(runner.admit_read(rid)["inflight"][0]["state"], "cancelled")
            self.assertEqual(list((self.root / "archive").iterdir()), [])
            cli.cmd_gc(gc)
            self.assertEqual(out.call_args.args[0]["archived"], [rid])
        with patch.object(runner.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(core.Refused, "admission .* is closed"):
                runner.cmd_exec(rid, sup.s["run"], ["true"])
            spawn.assert_not_called()
        self.cutover()

    def test_end_record_from_a_hyphenated_name_prefix_cannot_clear_another_users_inbox(self):
        other = core.USER + "-lab"
        rid = f"{other}-20261128-000000-run"
        (self.root / "inbox" / rid).mkdir()
        sup = self.supervisor()
        end = dict(sup.entry(), id=rid, state="ended", ended=core.now(), move_protocol=move.MOVE_PROTOCOL)
        (self.root / "archive" / f"{rid}-123.json").write_bytes(core.dumps(end))
        with core.Board.from_conf() as board, patch.object(board, "inbox_owner", return_value=other):
            with self.assertRaisesRegex(core.Refused, rf"run inbox without an end record: {rid}"):
                move.require_empty(board)

    def test_second_install_cannot_clear_an_inbox_while_first_install_starts(self):
        other = self.tmp / "other"
        env = dict(self.env, AGENT_BUS_CONFIG=str(other / "config"), AGENT_BUS_STATE=str(other / "state"),
                   AGENT_BUS_BIN=str(other / "bin" / "bus"), AGENT_BUS_BUILDS=str(other / "builds"))
        self.bus("_install", "--src", str(REPO), "--board", str(self.root), env=env, check=True)
        args = cli.parser().parse_args(["run", "--gpus", "0", "--detach", "true"])

        def start(rid, state):
            self.assertFalse((self.root / "agents" / f"{rid}.json").exists())
            other_adopt = subprocess.run([PY, "-IB", env["AGENT_BUS_BIN"], "adopt", "--all"], env=env,
                                         capture_output=True, text=True, timeout=10)
            self.assertEqual(other_adopt.returncode, 0, other_adopt.stderr)
            marker = self.root / "agents" / ".heartbeat" / f"{move.CLIENT_PREFIX}{core.USER}.json"
            self.assertTrue(json.loads(marker.read_bytes())["idle"])
            self.assertGreaterEqual(marker.stat().st_ctime, (self.root / "inbox" / rid).stat().st_ctime)
            result = self.cutover(check=False)
            self.assertIn("run inbox without an end record", result.stderr)
            self.assertIn(rid, result.stderr)
            self.assertFalse(self.target.exists())
            return 0, ""

        with patch.object(runner, "user_manager_ok", return_value=True), \
                patch.object(runner, "start_main_unit", side_effect=start) as main, \
                patch.object(runner, "cgroup_of", return_value="/fixture"), \
                patch.object(runner, "systemd_run", return_value=(0, "")):
            self.assertEqual(runner.cmd_run(args), 0)
            main.assert_called_once()

    def test_own_standing_booking_and_open_objection_window_refuse_move(self):
        t = core.now()
        info = self.bus_json("book", "--gpus", "0", "--from", str(t + 3600), "--to", str(t + 7200),
                             "--objection", "3s", "--json")
        result = self.cutover(check=False)
        self.assertIn("open objection windows", result.stderr)
        self.assertIn(info["id"], result.stderr)
        wait_for(lambda: core.now() >= info["objection_close"], 5, what="objection window closing")
        result = self.cutover(check=False)
        self.assertIn("pending or standing bookings", result.stderr)
        self.assertIn(info["id"], result.stderr)
        self.assertFalse(self.target.exists())
        self.bus("unbook", info["id"], check=True)
        self.cutover()

    def test_incomplete_legacy_booking_is_listed(self):
        t = core.now()
        with core.Board.from_conf() as board:
            with patch.object(bookings, "claim_receipt", side_effect=core.Refused("legacy writer interrupted")):
                with self.assertRaisesRegex(core.Refused, "legacy writer interrupted"):
                    bookings.publish_booking(board, [0], t + 600, t + 1200, objection_s=1)
        result = self.cutover(check=False)
        self.assertIn("booking revisions without receipts", result.stderr)
        self.assertTrue(self.root.exists())
        self.assertFalse(self.target.exists())

    def test_recent_foreign_author_and_newer_legacy_activity(self):
        foreign_uid = core.UID + 1
        log = self.root / "log" / "author.jsonl"
        log.write_text("{}\n")
        hb = self.root / "agents" / ".heartbeat" / (move.CLIENT_PREFIX + "author.json")
        hb.write_bytes(core.dumps(dict(user="author", build="v2.0.10", move_protocol=move.MOVE_PROTOCOL)))
        read = core.read_json
        original_stat = os.stat
        log_ctime = log.stat().st_ctime

        def by_owner(fd, name, *args):
            row = read(fd, name, *args)
            if name == hb.name and row:
                return row[0], SimpleNamespace(st_uid=foreign_uid, st_ctime=log_ctime + 1, st_mode=row[1].st_mode), row[2]
            return row

        def record_stat(path, *args, **kwargs):
            st = original_stat(path, *args, **kwargs)
            if path == "author.jsonl":
                return SimpleNamespace(st_uid=foreign_uid, st_mode=st.st_mode, st_ctime=log_ctime)
            return st

        with core.Board(str(self.root)) as old, patch.object(core.os, "stat", side_effect=record_stat), \
                patch.object(core, "owner_name", side_effect=lambda uid: "author" if uid == foreign_uid else core.USER):
            with self.assertRaisesRegex(core.Refused, r"move-capable build in the last 14 days: author"):
                move.require_empty(old)
            with patch.object(core, "read_json", side_effect=by_owner):
                move.require_empty(old)
                log_ctime += 2

                def older_marker(fd, name, *args):
                    row = by_owner(fd, name, *args)
                    if name == hb.name and row:
                        row[1].st_ctime -= 2
                    return row

                with patch.object(core, "read_json", side_effect=older_marker):
                    with self.assertRaisesRegex(core.Refused, r"author \(unreported\)"):
                        move.require_empty(old)

    def test_budget_carries_exact_intervals_without_receipts_or_seen_cache(self):
        t = core.now()
        with (self.root / "config").open("a") as f:
            f.write("budget_gpu_h=2\n")
        with core.Board.from_conf() as old:
            with patch.object(time, "time", return_value=t - 24 * 3600):
                bookings.publish_booking(old, [0, 1], t - 7200, t - 3600, objection_s=1)
        self.bus("adopt", "--all", check=True)
        self.assertTrue((self.home / "state" / "slots.json").is_file())
        with patch.object(core, "now", return_value=t + bookings.SEEN_GAP_S + 2):
            move.command(str(self.root), str(self.target))
        with self.installed_code(), core.Board.from_conf() as new:
            self.assertEqual(new.receipts(), {})
            self.assertEqual(bookings.evaluate(new), [])
            self.assertEqual(new.origin["usage"], {core.USER: [dict(start=t - 7200, end=t - 3600,
                                                                   gpus=2, charged=True)]})
            self.assertEqual(bookings.previous_usage(new, t, "another"), [])
            self.assertFalse(bookings.previous_usage(new, t - 3600 + new.limits["budget_span_s"], core.USER))
            with self.assertRaisesRegex(core.Refused, "over your budget"):
                bookings.publish_booking(new, [0], t + 3600, t + 7200, objection_s=1)
            b = bookings.publish_booking(new, [0], t + 7 * 86400, t + 7 * 86400 + 3600, objection_s=1)
            self.assertEqual(b["slot"], 1)
            self.assertNotEqual(bookings.seen_file(new), str(self.home / "state" / "slots.json"))
            self.assertEqual(bookings.evaluate(new)[0]["state"], "pending")
        self.assertTrue((self.retired / "seq" / "0" / "1").is_file())

    def test_lapsed_budget_ages_from_booking_end_not_cutover(self):
        t = core.now()
        start, end = t - 3600, t + 3600
        moved_at = t + bookings.SEEN_GAP_S + 2
        with (self.root / "config").open("a") as f:
            f.write("budget_gpu_h=2\nlapse_s=1\n")
        with core.Board.from_conf() as old, patch.object(time, "time", return_value=t - 7200):
            bookings.publish_booking(old, [0], start, end, objection_s=1)
        self.bus("adopt", "--all", check=True)
        self.assertEqual(self.bus_json("cal", "--all", "--json")["bookings"][0]["state"], "lapsed")
        with patch.object(core, "now", return_value=moved_at):
            move.command(str(self.root), str(self.target))
        with self.installed_code(), core.Board.from_conf() as new:
            span = new.limits["budget_span_s"]
            after_day_seven = moved_at + span + 5
            with patch.object(time, "time", return_value=after_day_seven):
                self.assertTrue(bookings.previous_usage(new, after_day_seven, core.USER))
                with self.assertRaisesRegex(core.Refused, "over your budget"):
                    bookings.publish_booking(new, [0, 1, 2, 3], after_day_seven, after_day_seven + 1800, objection_s=1)
            expiry = end + span
            with patch.object(time, "time", return_value=expiry):
                self.assertFalse(bookings.previous_usage(new, expiry, core.USER))
                bookings.publish_booking(new, [0, 1, 2, 3], expiry, expiry + 1800, objection_s=1)

    def test_aborted_transaction_restores_source_and_rerun_activates(self):
        with patch.object(move, "prepare", side_effect=OSError("fixture failure")):
            with self.assertRaisesRegex(core.Refused, "fixture failure"):
                move.command(str(self.root), str(self.target))
        self.assertEqual(stat.S_IMODE((self.root / "bookings").stat().st_mode), 0o1777)
        self.assertFalse((self.root / move.SEAL).exists())
        self.assertFalse(self.retired.exists())

        with patch.object(move, "activate", side_effect=OSError("fixture interruption")):
            with self.assertRaisesRegex(core.Refused, "fixture interruption"):
                move.command(str(self.root), str(self.target))
        self.assertTrue(self.retired.exists())
        self.assertTrue((self.target / core.MOVE_PENDING).exists())
        (self.target / "arrivals").chmod(0o700)
        self.cutover()
        self.assertEqual(stat.S_IMODE((self.target / "arrivals").stat().st_mode), 0o1777)
        self.assertEqual(self.bus_json("ls", "--json")["board"]["path"], str(self.target))

    def test_retry_repairs_partial_directories_and_refuses_foreign_owner(self):
        self.target.mkdir(mode=0o700)
        source = dict(path=self.pin["path"], inode=int(self.pin["inode"]), fsid=int(self.pin["fsid"]))
        (self.target / core.MOVE_PENDING).write_bytes(core.dumps(source))
        partial = [self.target / p for p in ("agents", "agents/.heartbeat", "seq", "seq/0")]
        for path in partial:
            path.mkdir(mode=0o700)
        ino, fstat = partial[-1].stat().st_ino, os.fstat

        def wrong_owner(fd):
            st = fstat(fd)
            if st.st_ino == ino:
                return SimpleNamespace(st_uid=st.st_uid + 1)
            return st

        with patch.object(os, "fstat", side_effect=wrong_owner), self.assertRaisesRegex(core.Refused, "belongs to"):
            move.command(str(self.root), str(self.target))
        self.assertTrue(self.root.exists())
        self.assertEqual(stat.S_IMODE(partial[-1].stat().st_mode), 0o700)
        self.cutover()
        for path in partial:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o1777)

    def test_retry_recovers_temp_from_interrupted_first_pending_publish(self):
        publish = core.publish

        def interrupt(fd, name, data, *args, **kwargs):
            if name == core.MOVE_PENDING:
                core._tmp(fd, data[:6], name=kwargs["temp_name"])
                raise OSError("interrupted marker")
            return publish(fd, name, data, *args, **kwargs)

        with patch.object(core, "publish", side_effect=interrupt):
            with self.assertRaisesRegex(core.Refused, "interrupted marker"):
                move.command(str(self.root), str(self.target))
        self.assertTrue(self.root.exists())
        temp, = self.target.iterdir()
        with core.Board(str(self.root)) as old:
            journal = move.owned_json(old, move.PREPARE)
        self.assertEqual(temp.name, f"{core.MOVE_PENDING}.{journal['nonce']}.tmp")
        self.assertFalse((self.target / core.MOVE_PENDING).exists())
        for name in (".tmp-" + "a" * 16, f"{core.MOVE_PENDING}.{'b' * 32}.tmp"):
            unrelated = self.target / name
            unrelated.write_bytes(temp.read_bytes())
            self.assertIn("destination must be empty", self.cutover(check=False).stderr)
            self.assertTrue(temp.exists())
            self.assertTrue(unrelated.exists())
            unrelated.unlink()
        self.cutover()
        self.assertFalse(temp.exists())

    def test_drain_waits_for_writer_and_seals_open_directories(self):
        self.bus("mailbox", "history", check=True)
        with core.Board.from_conf() as old:
            booking_fd = old.dir("bookings")
            mailbox_fd = old.dir("inbox", f"{core.USER}-history")
            call = subprocess.Popen([PY, "-IB", self.entry_point(), "move-board", str(self.root), str(self.target)],
                                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.15)
            self.assertIsNone(call.poll())
            # Keep independently opened descriptors after the shared transaction releases its lock.
            fds = [os.dup(booking_fd), os.dup(mailbox_fd)]
        try:
            output = call.communicate(timeout=10)
            self.assertEqual(call.returncode, 0, output)
            for fd in fds:
                self.assertEqual(os.fstat(fd).st_mode & 0o222, 0)
                if os.geteuid():
                    with self.assertRaises(PermissionError):
                        core.publish(fd, "late.json", b"{}")
        finally:
            for fd in fds:
                os.close(fd)

    def test_pending_upgrade_cannot_repin_until_confirmed(self):
        self.cutover()
        core.write_private(auto_upgrade.state_file(), {"pending": {"build": "candidate"}})
        self.assertNotEqual(self.bus("ls").returncode, 0)
        self.assertEqual(core.load_conf(), self.pin)
        self.assertNotIn("manual_build", auto_upgrade.read_state())
        core.write_private(auto_upgrade.state_file(), {"upgrades": [{"build": runner.installed_build()}]})
        self.assertEqual(self.bus_json("ls", "--json")["board"]["path"], str(self.target))

    def test_v209_manual_upgrade_repairs_confirmation_on_first_start(self):
        result = subprocess.run(["git", "archive", "--prefix=v209/", "d4a77f4", "lib", "bin"],
                                cwd=REPO, capture_output=True, check=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            archive.extractall(self.tmp)
        source = self.tmp / "v209"
        old = cli.copy_build(str(source))
        cli.flip(old)
        core.write_private(auto_upgrade.state_file(), {})
        self.bus("upgrade", "--build", str(REPO), check=True)
        build = runner.installed_build()
        self.assertNotEqual(build, Path(old).name)
        self.assertNotIn("manual_build", auto_upgrade.read_state())
        self.bus("adopt", "--all", check=True)
        self.assertEqual(auto_upgrade.read_state()["manual_build"], build)
        marker = self.root / "agents" / ".heartbeat" / f"{move.CLIENT_PREFIX}{core.USER}.json"
        self.assertTrue(move.capable(json.loads(marker.read_bytes())))
        self.cutover()
        self.assertEqual(self.bus_json("ls", "--json")["board"]["path"], str(self.target))

    def test_rolled_back_running_page_cannot_repin(self):
        self.cutover()
        candidate = Path(self.entry_point()).resolve().parents[1]
        previous = self.tmp / "previous"
        (previous / "bin").mkdir(parents=True)
        (previous / "bin" / "bus").touch()
        state = auto_upgrade.read_state()
        state["pending"] = dict(build=candidate.name, previous=str(previous), runs={}, commit="a" * 40, to="v9.0.0")
        core.write_private(auto_upgrade.state_file(), state)
        script = """
import sys
sys.path.insert(0, sys.argv[1])
from agentbus import core, page
conf = core.load_conf()
print("ready", flush=True)
sys.stdin.readline()
print(page.collect(conf, history=False)["board"])
"""
        with subprocess.Popen([PY, "-IB", "-c", script, str(candidate / "lib")], env=self.env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as p:
            try:
                self.assertEqual(p.stdout.readline().strip(), "ready")
                with auto_upgrade.lock():
                    auto_upgrade.rollback(state, cli.flip, "fixture")
                self.assertFalse(auto_upgrade.read_state().get("pending"))
                self.assertEqual(runner.build_root(core.BIN()), str(previous))
                output, error = p.communicate("\n", timeout=10)
                self.assertEqual(p.returncode, 0, error)
                self.assertEqual(output.strip(), "")
                self.assertEqual(core.load_conf(), self.pin)
            finally:
                if p.poll() is None:
                    p.kill()

    def test_exclusive_board_lock_times_out_and_private_cleanup_runs(self):
        sup = self.supervisor()
        with core.Board(str(self.root)).lease(exclusive=True), patch.object(core, "BOARD_LEASE_S", 0.03):
            sup.one_pass()
        sup.leader_exit_step.assert_called_once()
        self.assertFalse(sup.leader_exit_step.call_args.args[0].ok)
        self.assertIn("lock busy", sup.s["notices"][-1]["detail"])

    def test_install_checks_expected_owner_and_local_host(self):
        setup.setup_board(str(self.target), host=self.host(), blocks=1)
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
        sup.save()
        self.cutover()
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
        self.assertEqual(list((self.target / "agents").glob("*.json")), [])
        self.assertEqual(core.load_conf(), self.pin)
