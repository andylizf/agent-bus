#!/usr/bin/env python3
"""Exercise failed upgrade recovery using a private board and real systemd user units."""
import argparse
import os
import shlex
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "tests" / "v2"))
    from helpers import LINUX, SYSTEMD, BusTest, wait_for
    from agentbus import auto_upgrade, cli, core, runner

    if not LINUX or not SYSTEMD:
        raise SystemExit("the gate needs Linux and a working systemd user manager")

    class Gate(BusTest):
        def runTest(self):
            info = self.start("exec sleep 600")
            rid = info["id"]
            previous = runner.build_root(core.BIN())
            build_id = runner.build_id(previous)
            version = runner.build_version(previous)
            heartbeat = self.root / "agents" / ".heartbeat" / rid

            def job_identity():
                ok, props = runner.unit_query(info["main_unit"], "ActiveState", "InvocationID", "MainPID")
                self.assertTrue(ok, info)
                self.assertEqual(props["ActiveState"], "active")
                self.assertTrue(props["InvocationID"], props)
                self.assertGreater(int(props["MainPID"]), 0)
                return props["InvocationID"], props["MainPID"]

            original_job = job_identity()

            def healthy_after(mtime):
                state = self.state(rid)
                sup = state.get("supervisor") or {}
                if sup.get("code") != build_id or not sup.get("healthy") or heartbeat.stat().st_mtime_ns <= mtime:
                    return None
                self.assertEqual(state["run"], info["run"])
                self.assertEqual(heartbeat.read_text().strip(), version)
                self.assertEqual(core.proc_identity(sup["pid"]), {k: sup[k] for k in ("pid", "ticks", "boot_id")})
                return sup["pid"]

            wait_for(lambda: healthy_after(0), what="initial healthy supervisor")
            source = self.tmp / "broken-candidate"
            for part in ("bin", "lib"):
                shutil.copytree(Path(previous, part), source / part)
            marker = self.tmp / "candidate-reached-supervise"
            entry = source / "bin" / "bus"
            entry.write_text(
                "import sys\nfrom pathlib import Path\n"
                f"if sys.argv[1:2] == ['supervise']:\n    Path({str(marker)!r}).touch()\n    raise SystemExit(7)\n"
                + entry.read_text())
            candidate = cli.copy_build(str(source))
            trial = {"previous": previous, "build": runner.build_id(candidate), "commit": "a" * 40,
                     "from": version, "to": "gate-broken"}
            before = heartbeat.stat().st_mtime_ns
            with patch.object(auto_upgrade, "update", return_value=(candidate, trial)), \
                    patch.object(auto_upgrade, "TAKEOVER_S", 15):
                result, rc = auto_upgrade.check(cli.flip)
            self.assertEqual(rc, 1, result)
            self.assertIn("takeover deadline expired", result)
            self.assertIn("previous build supervising all live runs", result)
            self.assertTrue(marker.is_file(), "the deliberately broken supervisor never started")
            self.assertNotIn("pending", auto_upgrade.read_state())
            recovered_pid = wait_for(lambda: healthy_after(before), what="old-version heartbeat after rollback")
            self.assertEqual(job_identity(), original_job)
            print(f"rollback: main PID {original_job[1]}, supervisor {recovered_pid}, version {version}", flush=True)

            # Exercise the adopt service separately, with an orphan and no rollback worker to adopt it.
            marker.unlink()
            before = heartbeat.stat().st_mtime_ns
            cli.flip(candidate)
            wait_for(lambda: marker.is_file() and runner.lock_free(str(Path(runner.runs_dir(), f"{rid}.lock"))),
                     what="broken supervisor leaving the fixture job orphaned")
            self.assertEqual(job_identity(), original_job)
            cli.flip(previous)
            adopt = f"{self.prefix}-gate-adopt"
            with patch.dict(os.environ, XDG_CONFIG_HOME=str(self.home / "xdg"), AGENT_BUS_TIMER=adopt):
                units = auto_upgrade.unit_dir()
                units.mkdir(parents=True)
                (units / f"{adopt}.service").touch()
                auto_upgrade.install_timer()
                drop_in = (units / f"{adopt}.service.d" / "auto-upgrade.conf").read_text()
            command = next(line.partition("=")[2] for line in drop_in.splitlines()
                           if line.startswith("ExecStart=") and line != "ExecStart=")
            rc, output = runner.systemd_run([f"--unit={adopt}", "--wait", "--pipe", *runner.passthrough_env(),
                                             "--", *shlex.split(command)])
            self.assertEqual(rc, 0, output)
            adopted_pid = wait_for(lambda: healthy_after(before), what="old-version heartbeat after adopt service")
            self.assertNotEqual(adopted_pid, recovered_pid)
            self.assertEqual(job_identity(), original_job)
            print(f"adopt: main PID {original_job[1]}, supervisor {adopted_pid}, version {version}", flush=True)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Gate()]))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
