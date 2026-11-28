import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from helpers import REPO
from agentbus import auto_upgrade as au, cli, core, runner


class AutoUpgrade(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.env = dict(AGENT_BUS_BIN=str(self.root / "bin" / "bus"), AGENT_BUS_CONFIG=str(self.root / "config"),
                        AGENT_BUS_STATE=str(self.root / "state"), AGENT_BUS_BUILDS=str(self.root / "builds"),
                        XDG_CONFIG_HOME=str(self.root / "xdg"))
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        core.save_conf({"host": core.HOST})
        self.install_current("v2.0.8")
        self.now = 1_800_000_000
        clock = patch.object(au, "wall_time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.tags = {}
        self.fetch = None
        self.calls = []
        self.broken = False
        self.list_delay = 0
        self.delete_during_fetch = False

    def install_current(self, tag):
        target = self.root / "builds" / tag
        (target / "bin").mkdir(parents=True, exist_ok=True)
        (target / "bin" / "bus").touch()
        core.write_private(target / runner.BUILD_RELEASE, dict(tag=tag, base=tag))
        cli.flip(str(target))

    def fake_git(self, *args, binary=False):
        self.calls.append(args)
        if args[:2] == ("ls-remote", "--tags"):
            self.assertEqual(args[2], au.REPO)
            self.now += self.list_delay
            lines = []
            for tag, (oid, commit) in self.tags.items():
                lines.append(f"{oid}\trefs/tags/{tag}")
                if oid != commit:
                    lines.append(f"{commit}\trefs/tags/{tag}^{{}}")
            return "\n".join(lines)
        if args[0] == "init":
            return ""
        action = args[2]
        if action == "-c":
            self.assertEqual(args[3:5], ("fetch.fsckObjects=true", "fetch"))
            self.assertEqual(args[-2], au.REPO)
            if self.delete_during_fetch:
                raise core.Refused(f"git exited 128: fatal: couldn't find remote ref {args[-1]}")
            self.fetch = self.tags[args[-1].removeprefix("refs/tags/")] if self.fetch is None else self.fetch
            return ""
        if action == "rev-parse":
            return self.fetch[0 if args[3] == "FETCH_HEAD" else 1]
        if action == "ls-tree":
            self.assertEqual(args[4], self.fetch[1])
            self.fetch = None
            files = [b"100755 blob bus\tbin/bus", b"100644 blob lib\tlib/agentbus/__init__.py"]
            if getattr(self, "withdrawn", None):
                files.append(b"100644 blob withdrawn\tauto-upgrade-withdrawn")
            return b"\0".join(files) + b"\0"
        self.assertEqual(action, "cat-file")
        if args[-1] == "lib":
            return b""
        if args[-1] == "withdrawn":
            return self.withdrawn
        if self.broken:
            return b"raise SystemExit(7)\n"
        return b"""import json, sys
from pathlib import Path
if sys.argv[1] == '_upgrade-probe':
    print(json.dumps({'build': Path(__file__).resolve().parents[1].name, 'state_version': 1}))
"""

    def check(self):
        with patch.object(au, "git", side_effect=self.fake_git):
            return au.check(cli.flip)

    def test_soak_semver_and_annotated_tag(self):
        before = os.readlink(core.BIN())
        self.tags = {"v2.0.9": ("1" * 40, "1" * 40), "v2.0.10": ("a" * 40, "b" * 40),
                     "v2.0.100-rc1": ("2" * 40, "2" * 40)}
        self.assertEqual(self.check(), ("waiting for the 24 h soak", 0))
        self.now += au.SOAK_S - 1
        self.assertEqual(self.check()[1], 0)
        self.assertEqual(os.readlink(core.BIN()), before)
        self.assertEqual([call[0] for call in self.calls], ["ls-remote", "ls-remote"])
        self.now += 1
        self.assertEqual(self.check(), ("upgraded v2.0.8 to v2.0.10", 0))
        self.assertEqual(runner.build_version(runner.build_root(core.BIN())), "v2.0.10")
        status = au.doctor(False, False)
        self.assertEqual((status["current_tag"], status["last_check"], status["last_result"]),
                         ("v2.0.10", self.now, "upgraded v2.0.8 to v2.0.10"))
        self.assertIn("fallback", status["timer"])
        self.assertEqual(au.read_state()["upgrades"], [{"from": "v2.0.8", "to": "v2.0.10", "time": self.now,
                                                         "commit": "b" * 40, "build": runner.installed_build()}])
        self.now += au.SOAK_S
        self.assertEqual(self.check(), ("no newer release", 0))
        self.assertEqual(len(au.read_state()["upgrades"]), 1)

    def test_major_opt_in_and_downgrade_guard(self):
        self.tags = {"v3.0.0": ("a" * 40, "a" * 40), "v2.0.7": ("b" * 40, "b" * 40)}
        self.check()
        self.now += au.SOAK_S
        self.assertEqual(self.check(), ("no eligible update (newer major needs opt-in)", 0))
        self.assertEqual(runner.installed_build(), "v2.0.8")
        with patch.object(runner, "user_manager_ok", return_value=False):
            au.configure("on", allow_major=True)
        self.assertEqual(self.check(), ("upgraded v2.0.8 to v3.0.0", 0))
        self.tags.pop("v3.0.0")
        self.assertEqual(self.check(), ("no newer release", 0))
        self.assertEqual(au.doctor(True, True)["current_tag"], "v3.0.0")

    def test_retag_starts_new_soak_even_when_commit_is_unchanged(self):
        self.tags = {"v2.0.9": ("a" * 40, "b" * 40)}
        self.check()
        self.now += au.SOAK_S
        self.tags["v2.0.9"] = ("c" * 40, "b" * 40)
        self.assertEqual(self.check(), ("waiting for the 24 h soak", 0))
        self.assertEqual(runner.installed_build(), "v2.0.8")

    def test_soak_starts_when_tag_listing_arrives(self):
        self.tags = {"v2.0.9": ("a" * 40, "a" * 40)}
        self.list_delay = 5
        self.check()
        self.list_delay = 0
        self.now -= au.SOAK_S
        self.assertEqual(self.check(), ("waiting for the 24 h soak", 0))
        self.now += au.SOAK_S
        self.now += au.SOAK_S - 0.25
        self.assertEqual(self.check(), ("waiting for the 24 h soak", 0))
        self.now += 0.25
        self.assertEqual(self.check(), ("upgraded v2.0.8 to v2.0.9", 0))

    def test_changed_fetch_and_broken_build_keep_current(self):
        self.tags = {"v2.0.9": ("a" * 40, "b" * 40)}
        self.check()
        self.now += au.SOAK_S
        self.fetch = ("c" * 40, "b" * 40)
        result, rc = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("changed during fetch", result)
        self.assertEqual(au.doctor(False, False)["last_result"], result)
        self.assertEqual(runner.installed_build(), "v2.0.8")
        self.assertEqual(au.read_state()["seen"]["v2.0.9"], dict(oid="c" * 40, commit="b" * 40, first=self.now))
        self.fetch = None
        self.assertEqual(self.check(), ("waiting for the 24 h soak", 0))
        self.now += au.SOAK_S
        self.broken = True
        result, rc = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("exited 7", result)
        self.assertEqual(au.doctor(False, False)["last_result"], result)
        self.assertEqual(runner.installed_build(), "v2.0.8")

    def test_explicit_fetch_deletion_forgets_observation_even_if_tag_returns_unchanged(self):
        self.tags["v2.0.9"] = ("a" * 40, "a" * 40)
        self.check()
        self.now += au.SOAK_S
        self.delete_during_fetch = True
        result, rc = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("disappeared during fetch", result)
        self.assertNotIn("v2.0.9", au.read_state()["seen"])
        self.delete_during_fetch = False
        self.assertEqual(self.check(), ("waiting for the 24 h soak", 0))
        self.assertEqual(au.read_state()["seen"]["v2.0.9"]["first"], self.now)

    def test_network_failure_and_busy_lock(self):
        with patch.object(au, "git", side_effect=core.Refused("no network")):
            self.assertEqual(au.check(cli.flip), ("failed: no network", 1))
        with au.lock(shared=True):
            tools = self.root / "tools"
            tools.mkdir()
            for name, reply in (("systemctl", "running"), ("loginctl", "yes")):
                command = tools / name
                command.write_text(f"#!/bin/sh\necho {reply}\n")
                command.chmod(0o755)
            proc = subprocess.run([sys.executable, "-IB", str(REPO / "bin" / "bus"), "_auto-upgrade-timer"],
                                  capture_output=True, text=True, timeout=5,
                                  env=dict(os.environ, AGENT_BUS_SYSTEMD="on", PATH=f"{tools}:{os.environ['PATH']}"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("another upgrade is in progress", proc.stdout)
        self.assertEqual(au.doctor(False, False)["last_result"], "failed: no network")
        self.assertEqual(runner.installed_build(), "v2.0.8")

    def test_failed_activation_is_recorded_without_a_switch(self):
        self.tags = {"v2.0.9": ("a" * 40, "a" * 40)}
        self.check()
        self.now += au.SOAK_S
        with patch.object(cli, "flip", side_effect=OSError("cannot replace symlink")):
            self.assertEqual(self.check(), ("failed: v2.0.9 attempt 1/3, retry in 15 min: cannot replace symlink", 1))
        self.assertEqual(runner.installed_build(), "v2.0.8")
        self.assertNotIn("upgrades", au.read_state())
        self.assertIn("cannot replace symlink", au.doctor(False, False)["last_result"])

    def test_manual_check_waits_for_shared_service_result(self):
        def start(*args, timeout):
            self.assertEqual(args, ("start", "bus-auto-upgrade.service"))
            self.assertGreater(timeout, au.SERVICE_TIMEOUT_S)
            core.private_dir()
            core.write_private(au.state_file(), dict(last_check=self.now, last_result="no newer release"))
            return 0, "", ""

        args = Mock(action="check", allow_major=False)
        with patch.object(runner, "user_manager_ok", return_value=True), \
                patch.object(au, "linger_status", return_value="yes"), patch.object(runner, "sysctl", side_effect=start), \
                patch.object(au, "check", side_effect=AssertionError("foreground must never switch")):
            self.assertEqual(cli.cmd_auto_upgrade(args), 0)
        self.assertEqual(runner.installed_build(), "v2.0.8")

    def test_timer_default_opt_out_and_early_daily_schedule(self):
        au.unit_dir().mkdir(parents=True)
        (au.unit_dir() / "bus-adopt.service").touch()
        with patch.object(runner, "user_manager_ok", return_value=True), \
                patch.object(au, "linger_status", return_value="yes"), \
                patch.object(runner, "sysctl", return_value=(0, "", "")) as systemctl:
            self.assertEqual(au.configure("setup"), "on")
            systemctl.assert_called_with("enable", "--now", "bus-auto-upgrade.timer")
            timer = (au.unit_dir() / "bus-auto-upgrade.timer").read_text()
            service = (au.unit_dir() / "bus-auto-upgrade.service").read_text()
            self.assertIn(f"ConditionHost={core.HOST}", timer)
            self.assertIn("OnActiveSec=15min\nOnUnitActiveSec=1d", timer)
            self.assertIn(f'"{au.service_entry()}" _auto-upgrade-timer', service)
            self.assertIn(f'"{au.service_entry()}" _auto-upgrade-recover', service)
            adopt = (au.unit_dir() / "bus-adopt.service.d" / "auto-upgrade.conf").read_text()
            self.assertIn(f'"{au.service_entry()}" adopt --all', adopt)
            self.assertIn(f"TimeoutStartSec={au.RECOVERY_TIMEOUT_S}", adopt)
            self.assertEqual(au.configure("off"), "off")
            systemctl.assert_called_with("disable", "--now", "bus-auto-upgrade.timer")
        self.assertEqual(self.check(), ("off", 0))
        self.assertEqual(self.calls, [])
        self.assertFalse(au.doctor(False, False)["enabled"])

    def test_manual_switch_from_legacy_build_bootstraps_timer(self):
        env = dict(os.environ, AGENT_BUS_SYSTEMD="off")
        proc = subprocess.run([sys.executable, "-IB", str(REPO / "bin" / "bus"), "upgrade", "--build", str(REPO),
                               "--json"], capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["previous"], "v2.0.8")
        self.assertIn("timer unavailable", result["auto_upgrade"])
        self.assertTrue((au.unit_dir() / "bus-auto-upgrade.timer").exists())
        self.assertEqual(runner.installed_build(), result["installed"])
        self.assertEqual(runner.build_release(runner.build_root(core.BIN()))["base"], "v2.0.8")

    def test_real_git_peels_and_fetches_annotated_tag(self):
        repo, src = str(self.root / "repo"), str(self.root / "fetch")
        au.git("init", "--quiet", repo)
        Path(repo, "committed").write_text("tagged tree\n")
        au.git("-C", repo, "add", "committed")
        au.git("-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.org",
               "commit", "--quiet", "-m", "release")
        au.git("-C", repo, "-c", "user.name=Test", "-c", "user.email=test@example.org",
               "tag", "-a", "v2.0.9", "-m", "release")
        with patch.object(au, "REPO", Path(repo).as_uri()):
            oid, commit = au.release_refs()["v2.0.9"]
            self.assertNotEqual(oid, commit)
            au.fetch(src, "v2.0.9", oid, commit)
            au.git("-C", repo, "tag", "-d", "v2.0.9")
            with self.assertRaisesRegex(au.TagDeleted, "v2.0.9 disappeared during fetch"):
                au.fetch(str(self.root / "deleted"), "v2.0.9", oid, commit)
        self.assertFalse(Path(src, "committed").exists())
        self.assertEqual(au.git("-C", src, "show", f"{commit}:committed"), "tagged tree")

    def test_failed_takeover_rolls_back_and_newer_release_withdraws_older(self):
        self.tags = {"v2.0.9": ("a" * 40, "a" * 40), "v2.0.10": ("b" * 40, "b" * 40)}
        self.withdrawn = b"v2.0.9\n"
        self.check()
        self.now += au.SOAK_S

        def fails_on_candidate(build, active):
            self.assertEqual(runner.installed_build(), build)
            self.assertIn("verifying takeover", au.doctor(False, False)["last_result"])
            self.assertNotIn("upgrades", au.read_state())
            raise core.Refused("existing job did not take over")

        with patch.object(au, "wait_for_takeover", side_effect=fails_on_candidate):
            result, rc = self.check()
            self.assertEqual(rc, 1)
            self.assertIn("v2.0.10 attempt 1/3", result)
            self.assertNotIn("quarantine", au.read_state())
            self.assertIn("retry after", self.check()[0])
            self.now += au.RETRY_DELAYS_S[0]
            self.assertIn("attempt 2/3", self.check()[0])
            self.now += au.RETRY_DELAYS_S[1]
            result, rc = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("v2.0.10 quarantined after 3 failures: existing job did not take over", result)
        self.assertEqual(runner.installed_build(), "v2.0.8")
        self.assertIn("b" * 40, au.read_state()["quarantine"])
        self.assertEqual(self.check(), ("newer releases are withdrawn or quarantined", 0))
        self.assertEqual(len([a for a in self.calls if "fetch" in a]), 3)

    def test_slow_pass_can_succeed_on_retry(self):
        commit = "a" * 40
        self.tags["v2.0.9"] = (commit, commit)
        self.check()
        self.now += au.SOAK_S
        with patch.object(au, "wait_for_takeover", side_effect=core.Refused("slow pass")):
            self.assertIn("attempt 1/3", self.check()[0])
        before = os.readlink(core.BIN())
        self.assertIn("retry after", self.check()[0])
        self.assertEqual(os.readlink(core.BIN()), before)
        self.now += au.RETRY_DELAYS_S[0]
        self.assertEqual(self.check(), ("upgraded v2.0.8 to v2.0.9", 0))
        self.assertNotIn(commit, au.read_state()["retries"])

    def test_a_tag_that_reverts_has_to_soak_again(self):
        self.tags["v2.0.9"] = ("a" * 40, "b" * 40)
        self.check()
        self.now += au.SOAK_S - 1
        self.tags["v2.0.9"] = ("c" * 40, "b" * 40)
        self.check()
        self.now += 2
        self.tags["v2.0.9"] = ("a" * 40, "b" * 40)
        self.assertEqual(self.check(), ("waiting for the 24 h soak", 0))

    def copy_without_release_metadata(self):
        target = self.root / "builds" / ("a" * 12)
        for part in ("bin", "lib"):
            shutil.copytree(REPO / part, target / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return target

    def test_old_upgrader_copy_bootstraps_on_first_invocation(self):
        target = self.copy_without_release_metadata()
        cli.flip(str(target))
        proc = subprocess.run([sys.executable, "-IB", core.BIN(), "doctor", "--json"], capture_output=True,
                              text=True, env=dict(os.environ, AGENT_BUS_SYSTEMD="off"), timeout=10)
        status = json.loads(proc.stdout)["auto_upgrade"]
        self.assertIn("loginctl enable-linger", status["timer"])
        self.assertEqual(runner.build_release(str(target)), {"legacy_build": target.name})
        self.assertTrue(Path(au.service_entry()).is_file())
        self.assertTrue((au.unit_dir() / "bus-auto-upgrade.timer").is_file())
        self.assertTrue((au.unit_dir() / "timers.target.wants" / "bus-auto-upgrade.timer").is_symlink())

    def test_known_good_timer_recovers_abandoned_switch_with_broken_cli(self):
        old = self.copy_without_release_metadata()
        cli.flip(str(old))
        au.install_timer()
        bad = self.root / "builds" / "bad"
        (bad / "bin").mkdir(parents=True)
        (bad / "bin" / "bus").write_text("raise SystemExit(7)\n")
        core.write_private(au.state_file(), dict(pending={"previous": str(old), "from": "v2.0.8", "to": "v2.0.9",
                                                         "commit": "b" * 40, "build": "bad"}))
        cli.flip(str(bad))
        proc = subprocess.run([sys.executable, "-IB", au.service_entry(), "_auto-upgrade-recover"], capture_output=True,
                              text=True, env=dict(os.environ, AGENT_BUS_SYSTEMD="off"), timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(runner.installed_build(), old.name)
        self.assertIn("takeover was interrupted", au.doctor(False, False)["last_result"])

    def test_adopt_restores_stale_pending_then_adopts_on_previous_build(self):
        previous = runner.build_root(core.BIN())
        self.install_current("v2.0.9")
        pending = {"previous": previous, "from": "v2.0.8", "to": "v2.0.9", "build": "v2.0.9",
                   "commit": "b" * 40, "time": self.now - au.PENDING_STALE_S + 1}
        core.private_dir()
        core.write_private(au.state_file(), dict(pending=pending))

        def adopt():
            self.assertEqual(runner.installed_build(), "v2.0.8")
            return [{"started": True}]

        with patch.object(runner, "adopt_runs", side_effect=adopt) as sweep:
            self.assertEqual(runner.cmd_adopt_all(), [])
            sweep.assert_not_called()
            self.now += 1
            self.assertEqual(runner.cmd_adopt_all(), [{"started": True}])
        self.assertNotIn("pending", au.read_state())

    def test_services_follow_bus_after_legacy_only_rollback(self):
        au.install_timer()
        legacy = Path(core.BUILDS(), "v2.0.8", "bin", "bus")
        legacy.write_text("""import os, sys
from pathlib import Path
if sys.argv[1] == 'upgrade':
    link = Path(os.environ['AGENT_BUS_BIN'])
    temp = link.with_suffix('.new')
    temp.symlink_to(Path(sys.argv[3], 'bin', 'bus'))
    temp.replace(link)
elif sys.argv[1] == 'adopt':
    print('legacy adopt')
else:
    raise SystemExit(7)
""")
        bad = self.root / "builds" / "bad"
        (bad / "bin").mkdir(parents=True)
        (bad / "bin" / "bus").write_text("raise SystemExit(8)\n")
        cli.flip(str(bad))
        command = [sys.executable, "-IB"]
        subprocess.run([*command, str(legacy), "upgrade", "--build", str(legacy.parents[1])], check=True, timeout=5)
        adopt = subprocess.run([*command, au.service_entry(), "adopt", "--all"], capture_output=True,
                               text=True, check=True, timeout=5)
        self.assertEqual(adopt.stdout.strip(), "legacy adopt")
        upgrade = subprocess.run([*command, au.service_entry(), "_auto-upgrade-timer"], capture_output=True,
                                 text=True, check=True, timeout=5)
        self.assertIn("predates automatic upgrades", upgrade.stdout)
        Path(au.state_file()).write_text("unreadable journal")
        adopt = subprocess.run([*command, au.service_entry(), "adopt", "--all"], capture_output=True,
                               text=True, check=True, timeout=5)
        self.assertEqual(adopt.stdout.strip(), "legacy adopt")

    def test_rollback_adopts_and_verifies_each_live_run_with_no_adopt_timer(self):
        previous = runner.build_root(core.BIN())
        self.install_current("v2.0.9")
        active = {"run-a": "generation-a", "run-b": "generation-b"}
        pending = {"previous": previous, "from": "v2.0.8", "to": "v2.0.9", "build": "v2.0.9",
                   "commit": "b" * 40, "runs": active}
        core.private_dir()
        state = dict(pending=pending)

        def verify(build, runs):
            self.assertEqual((build, runs), ("v2.0.8", active))
            self.assertEqual(runner.installed_build(), "v2.0.8")
            self.assertIn("pending", au.read_state())

        with patch.object(runner, "adopt_runs") as adopt, patch.object(au, "wait_for_takeover", side_effect=verify):
            result = au.rollback(state, cli.flip, "candidate supervisor exited")
            adopt.assert_called_once_with(only=set(active))
        self.assertIn("previous build supervising all live runs", result)
        self.assertNotIn("pending", au.read_state())

    def test_rollback_records_unhealthy_restored_supervisor_and_can_resume(self):
        previous = runner.build_root(core.BIN())
        self.install_current("v2.0.9")
        commit = "b" * 40
        pending = {"previous": previous, "from": "v2.0.8", "to": "v2.0.9", "build": "v2.0.9",
                   "commit": commit, "runs": {"run-a": "generation"}}
        core.private_dir()
        with patch.object(runner, "adopt_runs"), patch.object(au, "wait_for_takeover") as verify:
            verify.side_effect = core.Refused("takeover deadline expired for run-a")
            result = au.rollback(dict(pending=pending), cli.flip, "candidate died")
            self.assertIn("rollback recovery failed: takeover deadline expired for run-a", result)
            self.assertIn("pending", au.read_state())
            self.assertEqual(runner.installed_build(), "v2.0.8")
            verify.side_effect = None
            self.assertIn("previous build supervising all live runs", au.settle(cli.flip))
        self.assertNotIn("pending", au.read_state())
        self.assertEqual(au.read_state()["retries"][commit]["attempts"], 1)

    def test_exclusive_upgrade_lock_keeps_launch_from_writing_run_state(self):
        command = [sys.executable, "-IB", str(REPO / "bin" / "bus"), "run", "--gpus", "0", "--", "true"]
        with au.lock():
            proc = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("holds the launch lock", proc.stderr)
        self.assertFalse(Path(core.STATE(), "runs").exists())

    def test_launch_arriving_during_download_defers_switch(self):
        commit = "a" * 40
        self.tags["v2.0.9"] = (commit, commit)
        self.check()
        self.now += au.SOAK_S
        upgrade = Mock(wraps=au.lock())
        upgrade.change_mode.side_effect = [True, False]
        with patch.object(au, "lock", return_value=upgrade):
            self.assertEqual(self.check(), ("skipped: a run is starting", 0))
        self.assertEqual(runner.installed_build(), "v2.0.8")
        self.assertNotIn("pending", au.read_state())
        self.assertNotIn("retries", au.read_state())

    def test_launch_refuses_already_loaded_candidate_after_rollback(self):
        installed = runner.build_root(core.BIN())
        candidate = str(Path(installed).with_name("candidate"))
        with patch.object(runner, "build_root", side_effect=[candidate, installed]), \
                patch.object(runner, "launch_run") as launch:
            with self.assertRaisesRegex(core.Refused, "installed build changed"):
                runner.cmd_run(Mock())
            launch.assert_not_called()

    def test_takeover_accepts_re_adoption_and_still_requires_healthy_live_process(self):
        identity = dict(pid=123, ticks=10, boot_id="test")
        state = dict(run="generation", state="running", supervisor=dict(identity, code="new", healthy=True))
        runs = {"test-run": "generation"}
        with patch.object(runner, "load_state", return_value=state), patch.object(runner, "lock_free", return_value=False), \
                patch.object(core, "proc_identity", return_value=identity), patch.object(au, "TAKEOVER_S", 0):
            au.wait_for_takeover("new", runs)
            state["supervisor"]["healthy"] = False
            with self.assertRaisesRegex(core.Refused, "deadline expired for test-run"):
                au.wait_for_takeover("new", runs)
            state["supervisor"]["healthy"] = True
            state["supervisor"]["pid"] = 124
            with self.assertRaisesRegex(core.Refused, "deadline expired for test-run"):
                au.wait_for_takeover("new", runs)
        stale = dict(run="generation", state="running", supervisor=None)
        new_identity = dict(identity, pid=124)
        state["supervisor"].update(new_identity)
        with patch.object(runner, "load_state", side_effect=[stale, state]), \
                patch.object(runner, "lock_free", return_value=False), \
                patch.object(core, "proc_identity", return_value=new_identity), \
                patch.object(runner, "adopt_runs") as adopt, patch.object(au.time, "sleep"):
            au.wait_for_takeover("new", runs)
            adopt.assert_called_once_with(only={"test-run"})

    def test_candidate_dry_adopts_without_writing_and_refuses_newer_state(self):
        rid = "test-run"
        path = Path(runner.state_path(rid))
        core.write_private(path, dict(host=core.HOST, state_version=runner.STATE_VERSION, id=rid))
        before = path.read_bytes()
        self.assertEqual(au.probe(str(REPO), [rid]), runner.STATE_VERSION)
        self.assertEqual(path.read_bytes(), before)
        core.write_private(path, dict(host=core.HOST, state_version=runner.STATE_VERSION + 1, id=rid))
        with self.assertRaisesRegex(core.Refused, "state version .* newer"):
            au.probe(str(REPO), [rid])

    def test_empty_run_reservation_does_not_block_upgrade_but_corrupt_state_does(self):
        path = Path(runner.state_path("test-run"))
        path.touch()
        self.assertEqual(au.live_runs(), [])
        path.write_text("unreadable private state")
        with self.assertRaisesRegex(core.Refused, "no run test-run"):
            au.live_runs()

    def test_verified_git_blobs_bypass_cache_and_refuse_symlinks(self):
        repo = self.root / "release"
        au.git("init", "--quiet", str(repo))
        (repo / "bin").mkdir()
        (repo / "lib" / "agentbus").mkdir(parents=True)
        bus = repo / "bin" / "bus"
        bus.write_bytes(b"#!/usr/bin/env python3\nprint('verified commit')  \n")
        bus.chmod(0o755)
        (repo / "lib" / "agentbus" / "__init__.py").write_bytes(b"\x23\xff\n")

        def commit():
            au.git("-C", str(repo), "add", ".")
            au.git("-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "test")
            return au.git("-C", str(repo), "rev-parse", "HEAD")

        good = commit()
        first, _ = au.stage_release(str(repo), "v2.0.9", good)
        (Path(first) / "bin" / "bus").write_text("cached tampered bytes\n")
        second, _ = au.stage_release(str(repo), "v2.0.9", good)
        self.assertNotEqual(first, second)
        self.assertEqual((Path(second) / "bin" / "bus").read_bytes(), bus.read_bytes())
        self.assertEqual((Path(second) / "lib" / "agentbus" / "__init__.py").read_bytes(), b"\x23\xff\n")
        installed = Path(core.BUILDS(), good[:12])
        shutil.copytree(second, installed)
        (installed / "bin" / "bus").chmod(0o700)
        (installed / "lib" / "agentbus" / "__init__.py").chmod(0o600)
        core.write_private(installed / runner.BUILD_RELEASE, dict(legacy_build=installed.name))
        cli.flip(str(installed))
        au.git("-C", str(repo), "tag", "v2.0.9", good)
        core.private_dir()
        with patch.object(au, "REPO", repo.as_uri()):
            self.assertEqual(au.installed_release(au.release_refs()), dict(tag="v2.0.9", base="v2.0.9", commit=good))
        (repo / "bin" / "link").symlink_to(self.root / "outside")
        bad = commit()
        with self.assertRaisesRegex(core.Refused, "not a regular file: bin/link"):
            au.stage_release(str(repo), "v2.0.10", bad)

    def test_reinstall_does_not_reuse_build_missing_release_metadata(self):
        short = "b" * 12
        old = Path(core.BUILDS(), short)
        (old / "bin").mkdir(parents=True)
        (old / "bin" / "bus").write_text("legacy copy")
        with patch.object(au, "git", side_effect=[short, ""]), \
                patch.object(au, "source_release", return_value=dict(tag="v2.0.9", base="v2.0.9")):
            result = cli.copy_build(str(REPO))
        self.assertNotEqual(result, str(old))
        self.assertEqual(runner.build_release(result)["tag"], "v2.0.9")
        self.assertEqual((old / "bin" / "bus").read_text(), "legacy copy")

    def test_supervisor_entry_and_heartbeat_publish_running_tag(self):
        with patch.object(runner, "build_root", return_value=runner.build_root(core.BIN())):
            sup = runner.Supervisor("run-id")
        sup.s = dict(id="run-id", host=core.HOST, gpus=[0], started=1, until=2, kind="other", run="a" * 32,
                     entry_state="running", **{"class": "backfill"})
        self.assertEqual(sup.entry()["build"], "v2.0.8")
        fd = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        self.assertTrue(core.touch(fd, sup.rid))
        self.assertTrue(core.touch(fd, sup.rid, data=(sup.version + "\n").encode()))
        self.assertEqual((self.root / sup.rid).read_text(), "v2.0.8\n")
        self.assertTrue(core.touch(fd, "new", data=b"v2.0.8\n"))
        self.assertEqual((self.root / "new").read_text(), "v2.0.8\n")
