import errno
import io
import json
import os
from pathlib import Path
import signal
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
                          log=str(self.log), gpus=[1, 2], exit_status=None,
                          units=dict(main="run-main.service", slice="run.slice", cgroup_path="/run.slice"))
        self.pin_file = Path(runner.ctl_dir(self.rid)) / "main-cgroup.json"
        self.props = dict(LoadState="loaded", MainPID="0", ExecMainCode="1", ExecMainStatus="0",
                          ExecMainExitTimestampMonotonic="200000", InvocationID="original", ControlGroup=self.path)
        self.pin = dict(run=self.sup.s["run"], path=self.path, invocation="original", boot="boot",
                        identity=[self.group.stat().st_dev, self.group.stat().st_ino])
        core.write_private(self.pin_file, self.pin)
        self.patch(runner, "CG_ROOT", new=str(self.root))
        self.patch(core, "boot_id", return_value="boot")
        self.query = self.patch(runner, "unit_query", side_effect=lambda unit, *keys: (
            True, {key: self.props[key] for key in keys if key in self.props}))
        self.patch(runner, "resolve_intents", return_value=[])
        self.sysctl = self.patch(runner, "sysctl", return_value=(0, "", ""))
        self.patch(runner, "close_admission")
        self.patch(runner.Supervisor, "track_escaped")
        self.gpu = self.patch(runner, "gpu_release_check", return_value=(True, [], [], None))
        self.clock = self.patch(runner.time, "monotonic", return_value=100)

    def set_populated(self, populated):
        for group in (self.group, self.group.parent):
            (group / "cgroup.events").write_text(f"populated {int(populated)}\nfrozen 0\n")

    def step(self):
        self.sup.leader_exit_step(runner._NoBoard())
        self.sup.save()

    def test_term_then_kill_with_adoption_and_gpu_verification(self):
        child = self.group / "child"
        child.mkdir()
        (self.group / "cgroup.procs").write_text("121\n122\n")
        (child / "cgroup.procs").write_text("123\n")
        (self.group.parent / "cgroup.procs").write_text("124\n")
        groups = {121: self.path, 122: "/moved.scope", 123: self.path + "/child", 124: "/run.slice"}
        handles, sent = {}, []

        def pidfd_open(pid):
            fd = os.open(os.devnull, os.O_RDONLY)
            handles[fd] = pid
            return fd

        def send(fd, sig):
            self.assertEqual(runner.load_state(self.rid)["exit_status"], 0)
            pid = handles[fd]
            sent.append((pid, sig))
            if pid == 121:
                (self.group / "cgroup.procs").write_text("")
            if sig == signal.SIGKILL:
                self.set_populated(False)

        def fake_open(path, *args, **kwargs):
            if str(path).startswith("/proc/"):
                return io.StringIO(f"comm-{path.split('/')[2]}\n")
            return open(path, *args, **kwargs)

        self.patch(core, "proc_cgroup", side_effect=lambda pid: groups.get(pid, "/app.slice/supervisor.service"))
        self.patch(runner, "open", create=True, side_effect=fake_open)
        opened = self.patch(os, "pidfd_open", create=True, side_effect=pidfd_open)
        self.patch(signal, "pidfd_send_signal", create=True, side_effect=send)
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
        self.assertEqual(sent[-1], (123, signal.SIGKILL))
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertEqual(self.sup.s["leader_remains"], ["GPU query failed"])
        self.assertNotIn(mock.call(124), opened.call_args_list)
        self.assertEqual([json.loads(line.removeprefix("leader-exit ")) for line in self.log.read_text().splitlines()],
                         [dict(pid=pid, comm=f"comm-{pid}", signal=sig.name) for pid, sig in sent])

        self.gpu.return_value = (True, [], [], None)
        self.step()
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
        self.props["InvocationID"] = "replacement"
        sent = self.patch(runner, "signal_job_cgroup")
        self.step()
        self.assertEqual(self.sup.s["state"], "stopping")
        self.assertIsNone(self.sup.s["exit_status"])
        self.assertIn("invocation no longer matches", self.sup.s["leader_remains"][0])
        sent.assert_not_called()
        self.gpu.assert_not_called()

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
        self.pin_file.unlink()
        self.props.update(ExecMainCode="2", ExecMainStatus="15")
        sent = self.patch(runner, "signal_job_cgroup")
        self.step()
        self.assertIn("no launch identity", self.sup.s["leader_remains"][0])
        self.gpu.assert_not_called()
        self.set_populated(False)
        self.step()
        sent.assert_not_called()
        self.assertEqual((self.sup.s["state"], self.sup.s["exit_status"]), ("ended", 143))

    def test_no_leftovers_go_straight_to_release_verification(self):
        self.set_populated(False)
        self.props["ExecMainStatus"] = "3"
        sent = self.patch(runner, "signal_job_cgroup")
        self.step()
        sent.assert_not_called()
        self.gpu.assert_called_once()
        self.assertEqual((self.sup.s["state"], self.sup.s["exit_status"]), ("ended", 3))
        self.assertIn(mock.call("stop", "run-main.service"), self.sysctl.call_args_list)

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
