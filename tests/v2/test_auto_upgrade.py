import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def install_current(self, tag):
        target = self.root / "builds" / tag
        (target / "bin").mkdir(parents=True, exist_ok=True)
        (target / "bin" / "bus").touch()
        core.write_private(target / runner.BUILD_RELEASE, dict(tag=tag, base=tag))
        cli.flip(str(target))

    def fake_git(self, *args):
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
        src, action = Path(args[1]), args[2]
        if action == "fetch":
            self.assertEqual(args[-2], au.REPO)
            self.fetch = self.tags[args[-1].removeprefix("refs/tags/")] if self.fetch is None else self.fetch
            return ""
        if action == "rev-parse":
            return self.fetch[0 if args[3] == "FETCH_HEAD" else 1]
        self.assertEqual(action, "checkout")
        self.assertEqual(args[-1], self.fetch[1])
        (src / "bin").mkdir()
        (src / "lib" / "agentbus").mkdir(parents=True)
        (src / "bin" / "bus").write_text("raise SystemExit(7)\n" if self.broken else "print('bus help')\n")
        self.fetch = None
        return ""

    def check(self):
        with patch.object(au, "git", side_effect=self.fake_git):
            return au.check(cli.copy_build, cli.flip)

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
        self.fetch = ("a" * 40, "c" * 40)
        result, rc = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("changed during fetch", result)
        self.assertEqual(au.doctor(False, False)["last_result"], result)
        self.assertEqual(runner.installed_build(), "v2.0.8")
        self.fetch = None
        self.broken = True
        result, rc = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("exited 7", result)
        self.assertEqual(au.doctor(False, False)["last_result"], result)
        self.assertEqual(runner.installed_build(), "v2.0.8")

    def test_network_failure_and_busy_lock(self):
        with patch.object(au, "git", side_effect=core.Refused("no network")):
            self.assertEqual(au.check(cli.copy_build, cli.flip), ("failed: no network", 1))
        with au.lock():
            proc = subprocess.run([sys.executable, "-IB", str(REPO / "bin" / "bus"), "auto-upgrade", "check"],
                                  capture_output=True, text=True, timeout=5, env=os.environ.copy())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("another upgrade is in progress", proc.stdout)
        self.assertEqual(au.doctor(False, False)["last_result"], "failed: no network")
        self.assertEqual(runner.installed_build(), "v2.0.8")

    def test_failed_activation_is_recorded_without_a_switch(self):
        self.tags = {"v2.0.9": ("a" * 40, "a" * 40)}
        self.check()
        self.now += au.SOAK_S
        with patch.object(cli, "flip", side_effect=OSError("cannot replace symlink")):
            self.assertEqual(self.check(), ("failed: cannot replace symlink", 1))
        self.assertEqual(runner.installed_build(), "v2.0.8")
        self.assertNotIn("upgrades", au.read_state())
        self.assertEqual(au.doctor(False, False)["last_result"], "failed: cannot replace symlink")

    def test_timer_default_opt_out_and_early_daily_schedule(self):
        with patch.object(runner, "user_manager_ok", return_value=True), \
                patch.object(au, "linger_status", return_value="yes"), \
                patch.object(runner, "sysctl", return_value=(0, "", "")) as systemctl:
            self.assertEqual(au.configure("setup"), "on")
            systemctl.assert_called_with("enable", "--now", "bus-auto-upgrade.timer")
            timer = (au.unit_dir() / "bus-auto-upgrade.timer").read_text()
            service = (au.unit_dir() / "bus-auto-upgrade.service").read_text()
            self.assertIn(f"ConditionHost={core.HOST}", timer)
            self.assertIn("OnActiveSec=15min\nOnUnitActiveSec=1d", timer)
            self.assertIn(f'"{core.BIN()}" auto-upgrade check', service)
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
            au.checkout(src, "v2.0.9", oid, commit)
        self.assertEqual(Path(src, "committed").read_text(), "tagged tree\n")
        self.assertEqual(au.git("-C", src, "rev-parse", "HEAD"), commit)

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
