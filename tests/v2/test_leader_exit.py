import errno
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

import helpers
from agentbus import core, runner


class LeaderExit(unittest.TestCase):
    def patch(self, obj, name, **kwargs):
        p = mock.patch.object(obj, name, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        env = mock.patch.dict(os.environ, AGENT_BUS_STATE=str(self.root / "state"))
        self.addCleanup(env.stop)
        env.start()
        self.path = "/run.slice/run-main.service"
        self.group = self.root / self.path[1:]
        self.group.mkdir(parents=True)
        self.set_populated(True)
        self.log = self.root / "run.log"
        self.rid = "test-leader-exit"
        self.sup = runner.Supervisor(self.rid)
        self.sup.hooks = mock.Mock()
        self.sup.s = dict(run="test-generation", mode="systemd", state="running", entry_state="running",
                          log=str(self.log), gpus=[1, 2], exit_status=None, main_pin_required=True,
                          units=dict(main="run-main.service", slice="run.slice", cgroup_path="/run.slice"))
        self.pin_file = Path(runner.ctl_dir(self.rid)) / "main-cgroup.json"
        self.props = dict(LoadState="loaded", ActiveState="active", MainPID="0", ExecMainCode="1", ExecMainStatus="0",
                          ExecMainExitTimestampMonotonic="200000", InvocationID="original", ControlGroup=self.path)
        self.pin = dict(run=self.sup.s["run"], path=self.path, invocation="original", boot="boot",
                        identity=[self.group.stat().st_dev, self.group.stat().st_ino])
        core.write_private(self.pin_file, self.pin)
        self.patch(runner, "CG_ROOT", new=str(self.root))
        self.patch(core, "boot_id", return_value="boot")
        self.slice_props = dict(LoadState="loaded", ActiveState="active", ControlGroup="/run.slice",
                                InvocationID="original-slice")
        self.query = self.patch(runner, "unit_query", side_effect=self.unit_query)
        self.patch(runner, "resolve_intents", return_value=[])
        self.sysctl = self.patch(runner, "sysctl", return_value=(0, "", ""))
        self.stopped = self.patch(runner, "stop_invocation", return_value=True)
        self.patch(runner, "close_admission")
        self.trace = self.patch(runner.Supervisor, "track_escaped")
        self.gpu = self.patch(runner, "gpu_release_check", return_value=(True, [], [], None))
        self.clock = self.patch(runner.time, "monotonic", return_value=100)

    def set_populated(self, populated):
        for group in (self.group, self.group.parent):
            (group / "cgroup.events").write_text(f"populated {int(populated)}\nfrozen 0\n")

    def unit_query(self, unit, *keys):
        props = self.slice_props if unit == "run.slice" else self.props
        return True, {key: props[key] for key in keys if key in props}

    def step(self):
        self.sup.leader_exit_step(runner._NoBoard())
        self.sup.save()

    def finish_after_stops(self):
        self.props["ActiveState"] = "inactive"
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.slice_props["ActiveState"] = "inactive"
        self.step()

    def test_term_then_kill_with_adoption_and_gpu_verification(self):
        child = self.group / "child"
        child.mkdir()
        (self.group / "cgroup.procs").write_text("121\n122\n")
        (child / "cgroup.procs").write_text("123\n")
        (self.group.parent / "cgroup.procs").write_text("124\n")
        handles, sent = {}, []
        kill = self.group / "cgroup.kill"
        kill.touch()
        write = os.write

        def write_control(fd, b):
            if os.fstat(fd).st_ino == kill.stat().st_ino:
                self.assertEqual(b, b"1")
                self.trace.assert_called()
                self.set_populated(False)
            return write(fd, b)

        def pidfd_open(pid):
            if pid == 122:
                (self.group / "cgroup.procs").write_text("")
            fd = os.open(os.devnull, os.O_RDONLY)
            handles[fd] = pid
            return fd

        def send(fd, sig):
            self.assertEqual(runner.load_state(self.rid)["exit_status"], 0)
            self.trace.assert_called()
            pid = handles[fd]
            sent.append((pid, sig))
            if pid == 121:
                (self.group / "cgroup.procs").write_text("122\n")

        def fake_open(path, *args, **kwargs):
            if str(path).startswith("/proc/"):
                return io.StringIO(f"comm-{path.split('/')[2]}\n")
            return open(path, *args, **kwargs)

        self.patch(core, "proc_cgroup", return_value="/app.slice/supervisor.service")
        self.patch(runner, "open", create=True, side_effect=fake_open)
        opened = self.patch(os, "pidfd_open", create=True, side_effect=pidfd_open)
        self.patch(signal, "pidfd_send_signal", create=True, side_effect=send)
        self.patch(os, "write", side_effect=write_control)
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertEqual(sent, [(121, signal.SIGTERM), (123, signal.SIGTERM)])
        self.gpu.assert_not_called()

        adopted = runner.Supervisor(self.rid, adopt=True)
        adopted.s = runner.load_state(self.rid)
        adopted.hooks = self.sup.hooks
        self.sup = adopted
        self.clock.return_value = 109.999
        self.step()
        self.assertEqual(len(sent), 2)
        self.gpu.assert_not_called()

        self.clock.return_value = 110
        self.gpu.return_value = (False, [], [], "GPU query failed")
        self.step()
        self.assertEqual(sent, [(121, signal.SIGTERM), (123, signal.SIGTERM)])
        self.assertEqual(kill.read_bytes(), b"1")
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertEqual(self.sup.s["leader_remains"], ["GPU query failed"])
        self.assertNotIn(mock.call(124), opened.call_args_list)
        records = [json.loads(line.removeprefix("leader-exit ")) for line in self.log.read_text().splitlines()]
        self.assertEqual(records, [dict(pid=pid, comm=f"comm-{pid}", signal=sig.name) for pid, sig in sent] + [
            dict(cgroup=self.path, signal="SIGKILL", source="cgroup.kill"),
            dict(pid=123, comm="comm-123", signal="SIGKILL", source="pre-kill snapshot")])

        self.gpu.return_value = (True, [], [], None)
        self.step()
        self.finish_after_stops()
        self.assertEqual((self.sup.s["state"], self.sup.s["exit_status"], self.sup.s["end_reason"]),
                         ("ended", 0, "exited rc=0"))

    def test_survivor_keeps_all_gpus_held(self):
        self.props["ExecMainStatus"] = "7"
        self.sup.s.update(held_gpus=[2], gpus_released=[1], gpus_released_checked=15)
        sent = self.patch(runner, "signal_job_cgroup", return_value="")
        self.step()
        self.clock.return_value = 110
        self.step()
        self.assertEqual([call.args[2] for call in sent.call_args_list], [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(self.sup.s["exit_status"], 7)
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertEqual(self.sup.s["leader_remains"], ["job cgroup still populated after SIGKILL"])
        self.assertEqual(self.sup.s["held_gpus"], [1, 2])
        self.assertEqual(self.sup.s["gpus_released"], [])
        self.assertNotIn("gpus_released_checked", self.sup.s)
        self.gpu.assert_not_called()

    def test_recreated_cgroup_never_signalled(self):
        self.group.rename(self.root / "old")
        self.group.mkdir()
        self.set_populated(True)
        sent = self.patch(runner, "signal_job_cgroup")
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertIn("no longer matches the launch record", self.sup.s["leader_remains"][0])
        sent.assert_not_called()
        self.gpu.assert_not_called()

    def test_different_invocation_never_signalled_or_used_for_status(self):
        self.props.update(InvocationID="replacement", ExecMainStatus="17")
        sent = self.patch(runner, "signal_job_cgroup")
        self.step()
        self.assertEqual(self.sup.s["state"], "needs-reconcile")
        self.assertIsNone(self.sup.s["exit_status"])
        self.assertIn("invocation no longer matches", self.sup.s["leader_remains"][0])
        sent.assert_not_called()
        self.gpu.assert_not_called()

    def test_replacement_still_running_is_reported(self):
        self.props.update(InvocationID="replacement", MainPID="129", ExecMainExitTimestampMonotonic="0")
        self.step()
        self.assertEqual(self.sup.s["state"], "needs-reconcile")
        self.assertIn("invocation no longer matches", self.sup.s["leader_remains"][0])
        self.gpu.assert_not_called()

    def test_missing_or_corrupt_required_pin_cannot_use_replacement_status(self):
        self.props.update(InvocationID="replacement", ExecMainStatus="17")
        self.pin_file.unlink()
        for contents in (None, "{bad json", '{"path": "/wrong"}'):
            with self.subTest(contents=contents):
                if contents:
                    self.pin_file.write_text(contents)
                self.step()
                self.assertEqual(self.sup.s["state"], "needs-reconcile")
                self.assertEqual(self.sup.s["held_gpus"], [1, 2])
                self.assertIsNone(self.sup.s["exit_status"])
                self.assertIn("launch identity is missing or unreadable", self.sup.s["leader_remains"][0])
        self.gpu.assert_not_called()
        self.stopped.assert_not_called()

    def test_unreadable_cgroup_is_not_proof_of_emptiness(self):
        read_file = core.read_file

        def unreadable(fd, name, *args):
            if name == "cgroup.events":
                raise PermissionError(errno.EACCES, "cgroup unreadable")
            return read_file(fd, name, *args)

        self.patch(core, "read_file", side_effect=unreadable)
        sent = self.patch(runner, "signal_job_cgroup")
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertIn("cgroup unreadable", self.sup.s["leader_remains"][0])
        sent.assert_not_called()
        self.gpu.assert_not_called()

    def test_old_run_with_no_pin_waits_without_signalling(self):
        self.sup.s.pop("main_pin_required")
        self.pin_file.unlink()
        self.props.update(ExecMainCode="2", ExecMainStatus="15")
        sent = self.patch(runner, "signal_job_cgroup")
        self.step()
        self.assertIn("no launch identity", self.sup.s["leader_remains"][0])
        self.gpu.assert_not_called()
        self.set_populated(False)
        self.step()
        sent.assert_not_called()
        self.finish_after_stops()
        self.assertEqual((self.sup.s["state"], self.sup.s["exit_status"]), ("ended", 143))
        self.stopped.assert_has_calls([mock.call("original"), mock.call("original-slice")])
        self.sysctl.assert_not_called()

    def test_inactive_job_without_invocation_cannot_supply_first_exit_status(self):
        self.set_populated(False)
        self.props.update(ActiveState="inactive", InvocationID="", ExecMainStatus="17")
        for requires_pin in (True, False):
            with self.subTest(requires_pin=requires_pin):
                if not requires_pin:
                    self.sup.s.pop("main_pin_required")
                    self.pin_file.unlink()
                self.step()
                self.assertEqual(self.sup.s["state"], "needs-reconcile")
                self.assertEqual(self.sup.s["held_gpus"], [1, 2])
                self.assertIsNone(self.sup.s["exit_status"])
                self.assertNotIn("end_reason", self.sup.s)
        self.stopped.assert_not_called()
        self.gpu.assert_not_called()

    def test_no_leftovers_go_straight_to_release_verification(self):
        self.set_populated(False)
        self.props["ExecMainStatus"] = "3"
        sent = self.patch(runner, "signal_job_cgroup")
        self.step()
        sent.assert_not_called()
        self.gpu.assert_called_once()
        self.finish_after_stops()
        self.assertEqual((self.sup.s["state"], self.sup.s["exit_status"]), ("ended", 3))
        self.stopped.assert_has_calls([mock.call("original"), mock.call("original-slice")])
        self.sysctl.assert_not_called()

    def test_late_replacement_blocks_reclamation(self):
        self.set_populated(False)

        def replace_on_gpu_check(*args):
            self.props.update(InvocationID="replacement", ExecMainStatus="17")
            return True, [], [], None

        self.gpu.side_effect = replace_on_gpu_check
        self.step()
        self.assertEqual(self.sup.s["state"], "needs-reconcile")
        self.assertIn("invocation changed during reclamation", self.sup.s["leader_remains"][0])
        self.assertEqual(self.sup.s["exit_status"], 0)
        self.stopped.assert_not_called()

    def test_slice_repopulated_on_final_check_holds_gpus(self):
        self.props["ActiveState"] = "inactive"
        self.set_populated(False)
        self.sup.s.update(held_gpus=[2], gpus_released=[1], gpus_released_checked=15)
        reads = iter(("empty", "empty", "populated"))
        with mock.patch.object(runner, "slice_state",
                               side_effect=lambda path: next(reads) if path == "/run.slice" else "empty"):
            self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertEqual(self.sup.s["leader_remains"], ["the run slice is no longer empty"])
        self.assertEqual(self.sup.s["held_gpus"], [1, 2])
        self.assertEqual(self.sup.s["gpus_released"], [])
        self.assertNotIn("gpus_released_checked", self.sup.s)
        self.stopped.assert_not_called()
        self.step()
        self.gpu.assert_called()
        self.stopped.assert_called_once_with("original-slice")
        self.assertEqual(self.sup.s["state"], "stopping")

    def test_stop_enqueue_and_slice_stop_failure_do_not_finish(self):
        self.set_populated(False)
        self.props["ExecMainStatus"] = "7"
        self.stopped.side_effect = [True, False, True]
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertIn("confirm the job unit is inactive", self.sup.s["leader_remains"][0])
        self.stopped.assert_called_once_with("original")

        self.props["ActiveState"] = "deactivating"
        self.step()
        self.stopped.assert_called_once_with("original")
        self.assertEqual(self.sup.s["state"], "stopping")
        self.props.update(ActiveState="inactive", InvocationID="", ExecMainStatus="17")
        self.step()
        self.stopped.assert_called_with("original-slice")
        self.assertEqual(self.sup.s["leader_remains"], ["systemd did not queue Stop for the run slice"])
        self.assertEqual(self.sup.s["state"], "stopping")
        self.step()
        self.assertEqual(self.sup.s["leader_remains"], ["waiting for systemd to confirm the run slice is inactive"])
        self.assertEqual(self.sup.s["state"], "stopping")

        self.slice_props["ActiveState"] = "deactivating"
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertEqual(self.stopped.call_count, 3)
        self.slice_props.update(LoadState="not-found", ActiveState="inactive", InvocationID="")
        self.step()
        self.assertEqual((self.sup.s["state"], self.sup.s["exit_status"]), ("ended", 7))

    def test_replaced_slice_during_reclamation_remains_held(self):
        self.set_populated(False)
        self.props["ActiveState"] = "inactive"
        self.step()
        self.assertEqual(runner.load_state(self.rid)["retire_invocations"],
                         {"main": "original", "slice": "original-slice"})
        self.slice_props["InvocationID"] = "replacement-slice"
        self.step()
        self.assertEqual(self.sup.s["state"], "needs-reconcile")
        self.assertIn("run slice's invocation changed", self.sup.s["leader_remains"][0])
        self.stopped.assert_called_once_with("original-slice")

    def test_slice_replaced_between_main_stop_and_next_pass_is_never_stopped(self):
        self.set_populated(False)

        def request_stop(invocation):
            self.assertEqual(invocation, "original")
            self.assertEqual(runner.load_state(self.rid)["retire_invocations"],
                             {"main": "original", "slice": "original-slice"})
            return True

        self.stopped.side_effect = request_stop
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.props.update(ActiveState="inactive", InvocationID="")
        self.slice_props["InvocationID"] = "replacement-slice"
        self.sup.s = runner.load_state(self.rid)
        self.step()
        self.assertEqual(self.sup.s["state"], "needs-reconcile")
        self.assertEqual(self.sup.s["held_gpus"], [1, 2])
        self.assertIn("run slice's invocation changed", self.sup.s["leader_remains"][0])
        self.stopped.assert_called_once_with("original")

    def test_stop_waits_for_both_invocations_to_be_saved(self):
        self.set_populated(False)
        save = self.sup.save

        def fail_invocation_save():
            if "retire_invocations" in self.sup.s:
                raise OSError("state write failed")
            save()

        with mock.patch.object(self.sup, "save", side_effect=fail_invocation_save):
            for _ in range(2):
                with self.assertRaisesRegex(OSError, "state write failed"):
                    self.step()
                self.assertNotIn("retire_invocations", self.sup.s)
        self.stopped.assert_not_called()
        self.step()
        self.stopped.assert_called_once_with("original")
        self.assertEqual(runner.load_state(self.rid)["retire_invocations"],
                         {"main": "original", "slice": "original-slice"})

    def test_failed_service_is_cleared_by_invocation_then_confirmed_inactive(self):
        self.set_populated(False)
        self.props.update(ActiveState="failed", ExecMainCode="2", ExecMainStatus="9")
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.stopped.assert_called_once_with("original", reset_failed=True)
        self.finish_after_stops()
        self.assertEqual((self.sup.s["state"], self.sup.s["exit_status"]), ("ended", 137))

    def test_traces_escapes_before_both_signals_and_holds_off_gpu_escape(self):
        escape = {"140": dict(pid=140, unit="escaped.scope", boot_id="boot", ticks=1)}
        self.trace.side_effect = lambda: self.sup.s.update(escaped=escape)
        self.patch(core, "identity_gone", return_value=False)

        def signal_cgroup(fd, path, sig, log):
            self.assertEqual(runner.load_state(self.rid)["escaped"], escape)
            self.trace.reset_mock()
            if sig == signal.SIGKILL:
                self.set_populated(False)
            return ""

        self.patch(runner, "signal_job_cgroup", side_effect=signal_cgroup)
        self.step()
        self.sup.s.pop("escaped")
        self.clock.return_value = 110
        self.step()
        self.assertEqual(self.sup.s["leader_remains"], ["job processes outside the slice: 140 (escaped.scope)"])
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertEqual(self.sup.s["held_gpus"], [1, 2])

    def test_threaded_child_does_not_block_grace_or_kernel_kill(self):
        threaded = self.group / "threaded"
        threaded.mkdir()
        (self.group / "cgroup.procs").write_text("123\n")
        (self.group / "cgroup.kill").touch()
        real_open = os.open

        def cgroup_open(path, flags, *args, **kwargs):
            at = kwargs.get("dir_fd")
            if path == "cgroup.procs" and at is not None and os.fstat(at).st_ino == threaded.stat().st_ino:
                raise OSError(errno.EOPNOTSUPP, "threaded cgroup")
            return real_open(path, flags, *args, **kwargs)

        self.patch(os, "open", side_effect=cgroup_open)
        self.patch(core, "proc_cgroup", return_value="/app.slice/supervisor.service")
        self.patch(runner, "process_comm", return_value="threaded-worker")
        self.patch(os, "pidfd_open", create=True, side_effect=lambda pid: os.open(os.devnull, os.O_RDONLY))
        sent = self.patch(signal, "pidfd_send_signal", create=True)
        self.step()
        self.assertEqual(self.sup.s["main_term_at"], 100)
        self.clock.return_value = 110
        self.step()
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[1], signal.SIGTERM)
        self.assertEqual((self.group / "cgroup.kill").read_bytes(), b"1")

    def test_kernel_kill_uses_held_directory_after_path_replacement(self):
        (self.group / "cgroup.procs").write_text("123\n")
        (self.group / "cgroup.kill").touch()
        old = self.root / "original-cgroup"
        self.patch(core, "proc_cgroup", return_value="/app.slice/supervisor.service")

        def replace(pid):
            self.group.rename(old)
            self.group.mkdir()
            (self.group / "cgroup.kill").touch()
            return "worker"

        self.patch(runner, "process_comm", side_effect=replace)
        fd = core.walk(str(self.group))
        try:
            runner.signal_job_cgroup(fd, self.path, signal.SIGKILL, self.log)
        finally:
            os.close(fd)
        self.assertEqual((old / "cgroup.kill").read_bytes(), b"1")
        self.assertEqual((self.group / "cgroup.kill").read_bytes(), b"")

    def test_log_fifo_never_opened_in_blocking_mode(self):
        os.mkfifo(self.log)
        read_fd = os.open(self.log, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, read_fd)
        real_open = os.open

        def checked_open(path, flags, *args, **kwargs):
            if path == str(self.log):
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_APPEND)
            return real_open(path, flags, *args, **kwargs)

        self.patch(os, "open", side_effect=checked_open)
        self.patch(core, "proc_cgroup", return_value="/app.slice/supervisor.service")
        opened = self.patch(os, "pidfd_open", create=True)
        self.patch(signal, "pidfd_send_signal", create=True)
        self.step()
        self.assertIn("job log must be a regular file", self.sup.s["leader_remains"][0])
        opened.assert_not_called()
        self.gpu.assert_not_called()

    def test_systemd_query_failure_during_cleanup_keeps_gpus_held(self):
        sent = self.patch(runner, "signal_job_cgroup", return_value="")
        self.step()
        self.query.side_effect = None
        self.query.return_value = (False, {})
        self.clock.return_value = 120
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertEqual(self.sup.s["leader_remains"], ["systemd did not answer about the job's unit"])
        self.gpu.assert_not_called()
        self.sysctl.assert_not_called()
        sent.assert_called_once()

    def test_supervisor_in_the_job_cgroup_blocks_cleanup(self):
        self.patch(core, "proc_cgroup", return_value=self.path)
        opened = self.patch(os, "pidfd_open", create=True)
        self.step()
        self.assertIn("supervisor is outside", self.sup.s["leader_remains"][0])
        opened.assert_not_called()
        self.gpu.assert_not_called()

    def test_job_pins_its_invocation_before_exec_and_cannot_replace_the_pin(self):
        self.pin_file.unlink()
        self.sup.save()
        spec = dict(cwd=str(self.root), command="exit 0", env={})
        core.write_private(runner.job_path(self.rid), spec)
        self.patch(core, "proc_cgroup", return_value=self.path)
        self.patch(os, "chdir")

        def execve(*args):
            self.assertEqual(core.read_private(self.pin_file), self.pin)

        launch = self.patch(os, "execve", side_effect=execve)
        with mock.patch.dict(os.environ, INVOCATION_ID="original"):
            runner.job_main(self.rid)
        with mock.patch.dict(os.environ, INVOCATION_ID="replacement"):
            with self.assertRaisesRegex(core.Refused, "already has a job cgroup identity"):
                runner.job_main(self.rid)
        launch.assert_called_once()
        self.assertEqual(core.read_private(self.pin_file), self.pin)

    def test_state_requires_pin_before_service_launch(self):
        self.sup.s.pop("main_pin_required")
        self.sup.s["cwd"] = str(self.root)
        self.sup.save()

        def launch(*args):
            self.assertTrue(runner.load_state(self.rid)["main_pin_required"])
            return 0, ""

        systemd = self.patch(runner, "systemd_run", side_effect=launch)
        self.assertEqual(runner.start_main_unit(self.rid, self.sup.s), (0, ""))
        systemd.assert_called_once()


class InvocationStop(unittest.TestCase):
    def test_stop_targets_only_the_invocation_object(self):
        invocation = "a0" * 16
        path = f"/org/freedesktop/systemd1/unit/{invocation}"
        lookup = subprocess.CompletedProcess([], 0, stdout=json.dumps({"type": "o", "data": [path]}))
        replaced = subprocess.CalledProcessError(1, ["busctl"], stderr=b"Unknown object")
        with mock.patch.object(subprocess, "run", side_effect=[lookup, replaced]) as run:
            self.assertFalse(runner.stop_invocation(invocation))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][-18:], ["ay", "16"] + ["160"] * 16)
        self.assertEqual(run.call_args_list[1].args[0][-5:],
                         [path, "org.freedesktop.systemd1.Unit", "Stop", "s", "ignore-dependencies"])
