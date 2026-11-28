#!/usr/bin/env python3
"""Exercise upgrade, rollback and adoption with a private board and real systemd user units."""
import argparse
import contextlib
import io
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    if not sys.platform.startswith("linux"):
        raise SystemExit("the gate needs Linux and a working systemd user manager")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "lib"))
    from agentbus import auto_upgrade, cli, core, runner

    class Gate(unittest.TestCase):
        def setUp(self):
            temp_root = tempfile.gettempdir()
            if os.statvfs(temp_root).f_flag & getattr(os, "ST_NOEXEC", 8):
                temp_root = str(Path.home())
            private = tempfile.TemporaryDirectory(prefix="agent-bus-gate-", dir=temp_root)
            self.addCleanup(private.cleanup)
            self.tmp = Path(private.name)
            self.board = self.tmp / "board"
            self.prefix = "abgate" + secrets.token_hex(8)
            fake_smi = self.tmp / "nvidia-smi"
            shutil.copyfile(root / "tests" / "v2" / "fake_nvidia_smi.py", fake_smi)
            fake_smi.chmod(0o700)
            gpus = self.tmp / "gpus.json"
            gpus.write_text(json.dumps({"gpus": [{"index": 0, "uuid": "GPU-0"}]}))
            env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BUS_")}
            env.update(AGENT_BUS_CONFIG=str(self.tmp / "config"), AGENT_BUS_STATE=str(self.tmp / "state"),
                       AGENT_BUS_BIN=str(self.tmp / "bin" / "bus"), AGENT_BUS_BUILDS=str(self.tmp / "builds"),
                       AGENT_BUS_UNIT_PREFIX=self.prefix, AGENT_BUS_NVIDIA_SMI=str(fake_smi),
                       AGENT_BUS_TEST_GPUS=str(gpus), AGENT_BUS_PASS_S="0.5",
                       XDG_CONFIG_HOME=str(self.tmp / "xdg"))
            environment = patch.dict(os.environ, env, clear=True)
            environment.start()
            self.addCleanup(environment.stop)
            self.assertTrue(runner.user_manager_ok(), "the gate needs a working systemd user manager")
            subprocess.run(["bash", str(root / "setup-board.sh"), str(self.board), socket.gethostname().split(".")[0],
                            "--gpus", "1", "--blocks", "3"], check=True, capture_output=True, text=True, timeout=30)
            self.bus_json("_install", "--src", str(root), "--board", str(self.board))
            self.units = {}
            self.addCleanup(self.clean_units)
            self.systemd_run = runner.systemd_run
            track = patch.object(runner, "systemd_run", side_effect=self.launch_unit)
            track.start()
            self.addCleanup(track.stop)

        def bus_json(self, *args):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = cli.main(args)
            self.assertEqual(rc, 0, out.getvalue())
            return json.loads(out.getvalue())

        def unit_props(self, name):
            return runner.unit_props(name, "InvocationID", "ActiveState", "ControlGroup")

        def record_unit(self, name, before=None):
            if before and before != "0" * 32:
                return
            invocation = self.unit_props(name).get("InvocationID")
            if invocation and invocation != "0" * 32:
                self.units[(name, invocation)] = None

        def launch_unit(self, args, **kwargs):
            options = args[:args.index("--")]
            names = []
            for arg in options:
                if arg.startswith("--slice="):
                    names.extend((self.prefix + ".slice", arg.partition("=")[2]))
            names.extend(arg.partition("=")[2] for arg in options if arg.startswith("--unit="))
            before = {name: self.unit_props(name).get("InvocationID") for name in names}
            try:
                return self.systemd_run(["--collect", *args], **kwargs)
            finally:
                for name in names:
                    self.record_unit(name, before[name])

        def active_invocation(self, name, invocation):
            props = self.unit_props(name)
            return props if props.get("InvocationID") == invocation and props.get("ActiveState") != "inactive" else None

        def clean_units(self):
            errors = []
            for name, invocation in reversed(self.units):
                try:
                    if name.endswith(".slice"):
                        def empty():
                            props = self.active_invocation(name, invocation)
                            return not props or runner.slice_state(props.get("ControlGroup")) in ("empty", "gone")

                        self.wait_for(empty, f"empty slice {name} ({invocation})")
                    props = self.active_invocation(name, invocation)
                    if props:
                        runner.stop_invocation(invocation, reset_failed=props.get("ActiveState") == "failed")
                        self.wait_for(lambda: not self.active_invocation(name, invocation),
                                      f"cleanup of {name} ({invocation})")
                except AssertionError as e:
                    errors.append(str(e))
            self.assertFalse(errors, "\n".join(errors))

        def wait_for(self, ready, what):
            end = time.monotonic() + 30
            while time.monotonic() < end:
                value = ready()
                if value:
                    return value
                time.sleep(0.25)
            self.fail(f"timed out waiting for {what}")

        def runTest(self):
            with patch.object(runner, "own_bus", return_value=os.path.realpath(core.BIN())):
                info = self.bus_json("run", "--gpus", "0", "--detach", "--", "exec sleep 600")
            rid = info["id"]
            previous = runner.build_root(core.BIN())
            version = runner.build_version(previous)
            heartbeat = self.board / "agents" / ".heartbeat" / rid

            def job_identity():
                ok, props = runner.unit_query(info["main_unit"], "ActiveState", "InvocationID", "MainPID")
                self.assertTrue(ok, info)
                self.assertEqual(props["ActiveState"], "active")
                self.assertTrue(props["InvocationID"], props)
                self.assertGreater(int(props["MainPID"]), 0)
                return props["InvocationID"], props["MainPID"]

            original_job = job_identity()

            def healthy_after(build, label, mtime):
                state = runner.load_state(rid)
                sup = state.get("supervisor") or {}
                if sup.get("code") != runner.build_id(build) or not sup.get("healthy") or not heartbeat.exists():
                    return None
                if heartbeat.stat().st_mtime_ns <= mtime or heartbeat.read_text().strip() != label:
                    return None
                self.assertEqual(state["run"], info["run"])
                self.assertEqual(core.proc_identity(sup["pid"]), {k: sup[k] for k in ("pid", "ticks", "boot_id")})
                return sup["pid"]

            def candidate(name, injected):
                source = self.tmp / name
                for part in ("bin", "lib"):
                    shutil.copytree(Path(previous, part), source / part)
                entry = source / "bin" / "bus"
                entry.write_text(injected + entry.read_text())
                return cli.copy_build(str(source))

            def trial(build, label, commit):
                plan = {"previous": previous, "build": runner.build_id(build), "commit": commit,
                        "from": version, "to": label}
                with patch.object(auto_upgrade, "update", return_value=(build, plan)), \
                        patch.object(auto_upgrade, "TAKEOVER_S", 15):
                    return auto_upgrade.check(cli.flip)

            self.wait_for(lambda: healthy_after(previous, version, 0), "initial healthy supervisor")
            marker = self.tmp / "candidate-reached-supervise"
            bad = candidate("broken-candidate", "import sys\nfrom pathlib import Path\n"
                            f"if sys.argv[1:2] == ['supervise']:\n    Path({str(marker)!r}).touch()\n"
                            "    raise SystemExit(7)\n")
            before = heartbeat.stat().st_mtime_ns
            result, rc = trial(bad, "gate-broken", "a" * 40)
            self.assertEqual(rc, 1, result)
            self.assertIn("takeover deadline expired", result)
            self.assertIn("previous build supervising all live runs", result)
            self.assertTrue(marker.is_file(), "the deliberately broken supervisor never started")
            self.assertNotIn("pending", auto_upgrade.read_state())
            recovered_pid = self.wait_for(lambda: healthy_after(previous, version, before), "heartbeat after rollback")
            self.assertEqual(job_identity(), original_job)
            print(f"rollback: main PID {original_job[1]}, supervisor {recovered_pid}, version {version}", flush=True)

            marker.unlink()
            before = heartbeat.stat().st_mtime_ns
            cli.flip(bad)
            self.wait_for(lambda: marker.is_file() and runner.lock_free(str(Path(runner.runs_dir(), f"{rid}.lock"))),
                          "broken supervisor leaving the fixture job orphaned")
            self.assertEqual(job_identity(), original_job)
            cli.flip(previous)
            adopt = f"{self.prefix}-adopt.service"
            with patch.dict(os.environ, AGENT_BUS_TIMER=adopt.removesuffix(".service")):
                units = auto_upgrade.unit_dir()
                units.mkdir(parents=True)
                (units / adopt).touch()
                auto_upgrade.install_timer()
                drop_in = (units / f"{adopt}.d" / "auto-upgrade.conf").read_text()
            command = next(line.partition("=")[2] for line in drop_in.splitlines()
                           if line.startswith("ExecStart=") and line != "ExecStart=")
            rc, output = runner.systemd_run([f"--unit={adopt}", "--wait", "--pipe", *runner.passthrough_env(),
                                             "--", *shlex.split(command)])
            self.assertEqual(rc, 0, output)
            adoptions = json.loads(output)
            for adoption in adoptions:
                if adoption.get("unit"):
                    self.record_unit(adoption["unit"])
            self.assertTrue(any(a.get("id") == rid and a.get("started") for a in adoptions), output)
            adopted_pid = self.wait_for(lambda: healthy_after(previous, version, before), "heartbeat after adopt service")
            self.assertNotEqual(adopted_pid, recovered_pid)
            self.assertEqual(job_identity(), original_job)
            print(f"adopt: main PID {original_job[1]}, supervisor {adopted_pid}, version {version}", flush=True)

            label = "v9999.0.1" if version != "v9999.0.1" else "v9999.0.2"
            good = candidate("healthy-candidate", "# Healthy gate candidate\n")
            core.write_private(Path(good, runner.BUILD_RELEASE), dict(tag=label, base=label, commit="b" * 40))
            before = heartbeat.stat().st_mtime_ns
            result, rc = trial(good, label, "b" * 40)
            self.assertEqual(rc, 0, result)
            self.assertIn(f"upgraded {version} to {label}", result)
            new_pid = self.wait_for(lambda: healthy_after(good, label, before), "new-version heartbeat after upgrade")
            self.assertEqual(job_identity(), original_job)
            print(f"upgrade: main PID {original_job[1]}, supervisor {new_pid}, version {label}", flush=True)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Gate()]))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
