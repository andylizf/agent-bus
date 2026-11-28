#!/usr/bin/env python3
"""Verify a board move with two private installs, real systemd supervisors and a fake GPU."""
import argparse
import datetime
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tools"))

from agentbus import auto_upgrade, cli, core, move, runner
from auto_upgrade_gate import healthy_supervisor_pid


def report(message):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{now} {message}", flush=True)


class Gate(unittest.TestCase):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.clients = []

    def private_temp(self, parent):
        directory = tempfile.TemporaryDirectory(prefix="agent-bus-move-gate-", dir=parent)
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def command(self, script, *args, env=None, expected=0):
        result = subprocess.run([sys.executable, "-IB", str(script), *map(str, args)], env=env,
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stderr if expected else result.stdout

    def wait_for(self, test, what):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = test()
            if value:
                return value
            time.sleep(0.2)
        self.fail(f"timed out waiting for {what}")

    def state(self, client):
        return core.read_private(client["info"]["state_file"]) or {}

    def identity(self, client):
        props = runner.unit_props(client["info"]["main_unit"], "InvocationID", "MainPID", "ActiveState")
        self.assertEqual(props.get("ActiveState"), "active", props)
        self.assertGreater(int(props["MainPID"]), 0)
        self.assertTrue(props["InvocationID"])
        return props["MainPID"], props["InvocationID"]

    def healthy(self, client, board, after_ns=0):
        state = self.state(client)
        self.assertEqual(state.get("run"), client["info"]["run"])
        if state.get("board", {}).get("path") != str(board):
            return None
        sup = state.get("supervisor") or {}
        pid = healthy_supervisor_pid(sup, Path(client["build"]).name, after_ns, core.proc_identity)
        rid = client["info"]["id"]
        heartbeat = board / "agents" / ".heartbeat" / rid
        entry = board / "agents" / f"{rid}.json"
        pin = core.load_conf(str(Path(client["env"]["AGENT_BUS_CONFIG"], "board.conf")))
        if pid is None or pin.get("path") != str(board):
            return None
        try:
            row = json.loads(entry.read_bytes())
            if (heartbeat.read_text().strip() == client["label"] and row.get("build") == client["label"]
                    and (not client.get("upgraded") or move.capable(row))):
                return pid
        except FileNotFoundError:
            return None

    def start(self, name):
        home = self.workspace / name
        home.mkdir()
        env = dict(self.env, AGENT_BUS_CONFIG=str(home / "config"), AGENT_BUS_STATE=str(home / "state"),
                   AGENT_BUS_BIN=str(home / "bin" / "bus"), AGENT_BUS_BUILDS=str(home / "builds"),
                   AGENT_BUS_UNIT_PREFIX=self.prefix + name, XDG_CONFIG_HOME=str(home / "xdg"))
        client = dict(env=env, done=home / "done")
        self.clients.append(client)
        install = self.command(self.args.previous / "bin" / "bus", "_install", "--src", self.args.previous,
                               "--board", self.old, env=env)
        client["bus"] = Path(json.loads(install)["bin"])
        client["build"] = str(client["bus"].resolve().parents[1])
        client["label"] = runner.build_version(client["build"])
        done = shlex.quote(str(client["done"]))
        command = f"while [ ! -e {done} ] && [ \"$SECONDS\" -lt 120 ]; do sleep 0.2; done"
        client["info"] = json.loads(self.command(client["bus"], "run", "--gpus", "0", "--slug", name,
                                                  "--detach", "--", command, env=env))
        self.assertEqual(client["info"]["mode"], "systemd")
        client["original_job"] = self.identity(client)
        self.wait_for(lambda: self.healthy(client, self.old), f"{name} supervisor on source")
        return client

    def upgrade(self, client, board):
        with patch.dict(os.environ, client["env"], clear=True):
            candidate = cli.copy_build(str(ROOT))
            label = "v9999.0.1"
            commit = "b" * 40
            core.write_private(Path(candidate, runner.BUILD_RELEASE), dict(tag=label, base=label, commit=commit))
            plan = dict(previous=client["build"], build=Path(candidate).name, commit=commit,
                        **{"from": client["label"], "to": label})
            after_ns = time.monotonic_ns()
            with patch.object(auto_upgrade, "update", return_value=(candidate, plan)), \
                    patch.object(auto_upgrade, "TAKEOVER_S", 15):
                result, rc = auto_upgrade.check(cli.flip)
            self.assertEqual(rc, 0, result)
            self.assertEqual(result, f"upgraded {client['label']} to {label}")
            client.update(build=candidate, label=label, upgraded=True)
            pid = self.wait_for(lambda: self.healthy(client, board, after_ns), f"upgraded supervisor on {board}")
            self.assertEqual(self.identity(client), client["original_job"])
            return pid

    def clean(self):
        for client in self.clients:
            client["done"].touch()
        try:
            for client in self.clients:
                if "info" in client:
                    self.wait_for(lambda: self.state(client).get("state") == "ended", "fixture job ending normally")
                    info = client["info"]
                    units = self.state(client)["units"]
                    for name in (info["main_unit"], units["sup"], info["slice"]):
                        self.wait_for(lambda: runner.unit_props(name, "ActiveState").get("ActiveState") == "inactive",
                                      f"inactive fixture unit {name}")
        finally:
            if self.archive.exists():
                with core.Board(str(self.archive)) as board:
                    for name, mode in move.owned_json(board, move.SEAL, core.MOVE_LIMIT).items():
                        move.directory_mode(board, name.split("/"), mode)
        report("cleanup: fixture jobs exited naturally and their units are inactive")

    def runTest(self):
        self.assertTrue(runner.user_manager_ok(), "the gate needs a working systemd user manager")
        self.workspace = self.private_temp(self.args.destination_parent)
        self.old = self.private_temp(self.args.source_parent) / "board"
        self.new = self.workspace / "board"
        self.prefix = "abmove" + secrets.token_hex(6)
        fake = self.workspace / "nvidia-smi"
        shutil.copyfile(ROOT / "tests" / "v2" / "fake_nvidia_smi.py", fake)
        fake.chmod(0o700)
        gpus = self.workspace / "gpus.json"
        gpus.write_text(json.dumps({"gpus": [{"index": 0, "uuid": "GPU-0"}]}))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BUS_")}
        self.env.update(AGENT_BUS_NVIDIA_SMI=str(fake), AGENT_BUS_TEST_GPUS=str(gpus), AGENT_BUS_PASS_S="0.5",
                        AGENT_BUS_CONFIG=str(self.workspace / "operator-config"),
                        AGENT_BUS_STATE=str(self.workspace / "operator-state"))
        from agentbus import setup
        for board in (self.old, self.new):
            setup.setup_board(str(board), host=core.HOST, gpus=1, blocks=3)
            with (board / "config").open("a") as f:
                f.write("min_objection_s=1\n")
        with core.Board(str(self.old)) as old, core.Board(str(self.new)) as new:
            self.archive = Path(move.retired(old.pin_fields()))
            report(f"start: {core.HOST}, source fsid {old.fsid}, destination fsid {new.fsid}, previous {self.args.previous}")
        self.addCleanup(self.clean)
        early = self.start("early")
        late = self.start("late")
        self.upgrade(early, self.old)
        refusal = self.command(ROOT / "bin" / "bus", "move-board", "stage", self.old, self.new,
                               env=self.env, expected=1)
        self.assertIn("runs without a move-capable build", refusal)
        self.assertIn(late["info"]["id"], refusal)
        report("stage refused while one real supervisor was still on the pre-move build")
        self.upgrade(late, self.old)
        self.command(ROOT / "bin" / "bus", "move-board", "stage", self.old, self.new, env=self.env)
        t = core.now()
        booked = json.loads(self.command(late["bus"], "book", "--gpus", "0", "--from", t + 3600, "--to", t + 7200,
                                          "--objection", "3s", "--json", env=late["env"]))
        refusal = self.command(ROOT / "bin" / "bus", "move-board", "finish", self.old, self.new, "--wait", 0,
                               env=self.env, expected=1)
        self.assertIn("open objection windows", refusal)
        report("finish refused a booking opened after stage")
        self.wait_for(lambda: core.now() >= booked["objection_close"], "objection window closing")
        afd = core.walk(str(self.old / "arrivals"))
        mailbox = core.walk(str(self.old / "inbox" / late["info"]["id"]))
        try:
            report(self.command(ROOT / "bin" / "bus", "move-board", "finish", self.old, self.new, "--wait", 0,
                                env=self.env).strip())
            with self.assertRaises(PermissionError):
                core.publish(afd, "test-arrival.json", b"{}")
            with self.assertRaises(PermissionError):
                core.publish(mailbox, "test-mail.json", b"{}")
        finally:
            os.close(afd)
            os.close(mailbox)
        early_pid = self.wait_for(lambda: self.healthy(early, self.new), "early supervisor re-pinning")
        late_pid = self.wait_for(lambda: self.healthy(late, self.new), "late supervisor re-pinning")
        self.assertEqual(self.identity(early), early["original_job"])
        self.assertEqual(self.identity(late), late["original_job"])
        report(f"cutover: early main PID {early['original_job'][0]}, supervisor {early_pid}; "
               f"late main PID {late['original_job'][0]}, supervisor {late_pid}")
        cal = json.loads(self.command(late["bus"], "cal", "--json", env=late["env"]))
        replayed = next(b for b in cal["bookings"] if b["id"] == booked["id"])
        self.assertEqual((replayed["slot"], replayed["revision"]), (booked["slot"], booked["revision"]))
        self.assertEqual(replayed["objection_close"], booked["objection_close"])
        report(f"replay: build {late['label']}, original receipt {replayed['slot']} and objection close preserved")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True, help="source tree of the pre-move build")
    parser.add_argument("--source-parent", type=Path, help="parent for a private source directory, for example GPFS")
    parser.add_argument("--destination-parent", type=Path, help="parent for a private destination directory, for example XFS")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("requires Linux and a working systemd user manager")
    args.previous = args.previous.resolve(strict=True)
    if not (args.previous / "bin" / "service-entry.py").is_file():
        parser.error("the pre-move source tree must include automatic upgrades")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Gate(args)]))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
