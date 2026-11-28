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

from agentbus import auto_upgrade, bookings, cli, core, move, runner, setup
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

    def wait_for(self, test, what, timeout=20):
        deadline = time.monotonic() + timeout
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

    def install(self, name):
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
        return client

    def start(self, client):
        done = shlex.quote(str(client["done"]))
        command = f"while [ ! -e {done} ] && [ \"$SECONDS\" -lt 120 ]; do sleep 0.2; done"
        client["info"] = json.loads(self.command(client["bus"], "run", "--gpus", "0", "--slug", "fixture",
                                                  "--detach", "--", command, env=client["env"]))
        self.assertEqual(client["info"]["mode"], "systemd")
        client["original_job"] = self.identity(client)
        self.wait_for(lambda: self.healthy(client, self.old), "fixture supervisor on source")

    def upgrade(self, client):
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
            if "info" in client:
                self.wait_for(lambda: self.healthy(client, self.old, after_ns), "move-capable supervisor on source")
                self.assertEqual(self.identity(client), client["original_job"])

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
        setup.setup_board(str(self.old), host=core.HOST, gpus=1, blocks=3)
        with (self.old / "config").open("a") as f:
            f.write("min_objection_s=1\nbudget_gpu_h=1\n")
        with core.Board(str(self.old)) as old:
            self.archive = Path(move.retired(old.pin_fields()))
            report(f"start: {core.HOST}, source fsid {old.fsid}, target filesystem {os.statvfs(self.workspace).f_fsid}, "
                   f"previous {self.args.previous}")
        self.addCleanup(self.clean)
        early = self.install("early")
        late = self.install("late")
        self.start(early)
        self.upgrade(early)
        refused = lambda: self.command(ROOT / "bin" / "bus", "move-board", self.old, self.new, env=self.env, expected=1)
        refusal = refused()
        self.assertIn("runs (including unknown occupancy)", refusal)
        self.assertIn(early["info"]["id"], refusal)
        self.assertFalse(self.new.exists())
        report(f"move refused live run {early['info']['id']} after its confirmed supervisor upgrade")
        early["done"].touch()
        self.wait_for(lambda: self.state(early).get("state") == "ended", "fixture run ending naturally")

        t = core.now()
        booked = json.loads(self.command(early["bus"], "book", "--gpus", "0", "--from", t + 3600, "--to", t + 7200,
                                         "--objection", "3s", "--json", env=early["env"]))
        self.assertIn("open objection windows", refused())
        self.wait_for(lambda: core.now() >= booked["objection_close"], "objection window closing")
        self.assertIn("pending or standing bookings", refused())
        self.assertFalse(self.new.exists())
        report(f"move refused open objection window and standing booking {booked['id']}")
        self.command(early["bus"], "unbook", booked["id"], env=early["env"])
        objection_s = 1
        with patch.dict(os.environ, early["env"], clear=True), core.Board.from_conf() as old:
            with patch.object(time, "time", return_value=t - 86400):
                historical = bookings.publish_booking(old, [0], t - 7200, t - 3600, objection_s=objection_s)
            receipt = old.receipts()[historical["slot"]]
        rid = early["info"]["id"]
        archived, = (self.old / "archive").glob(f"{rid}-*.json")
        archived.write_bytes(core.dumps(dict(json.loads(archived.read_bytes()), state="running")))
        self.assertIn("run inbox without an end record", refused())
        self.command(early["bus"], "adopt", "--all", env=early["env"])
        self.assertIn("run inbox without an end record", refused())
        gc = json.loads(self.command(early["bus"], "gc", "--mine", "--json", env=early["env"]))
        self.assertEqual(gc["archived"], [rid])
        report("an idle report left the unarchived inbox blocked until owner gc wrote its end record")
        ready_at = receipt["_ctime"] + bookings.SEEN_GAP_S + objection_s + 1
        self.wait_for(lambda: core.now() >= ready_at, "all readers' historical objection windows closing", timeout=40)
        afd = core.walk(str(self.old / "arrivals"))
        mailbox = core.walk(str(self.old / "inbox" / early["info"]["id"]))
        try:
            report(self.command(ROOT / "bin" / "bus", "move-board", self.old, self.new, env=self.env).strip())
            with self.assertRaises(PermissionError):
                core.publish(afd, "test-arrival.json", b"{}")
            with self.assertRaises(PermissionError):
                core.publish(mailbox, "test-mail.json", b"{}")
        finally:
            os.close(afd)
            os.close(mailbox)
        self.command(late["bus"], "ls", "--json", env=late["env"], expected=1)
        conf = Path(late["env"]["AGENT_BUS_CONFIG"], "board.conf")
        self.assertEqual(core.load_conf(str(conf))["path"], str(self.old))
        self.upgrade(late)
        for client in (early, late):
            result = json.loads(self.command(client["bus"], "ls", "--json", env=client["env"]))
            self.assertEqual(result["board"], dict(path=str(self.new), identity="ok"))
            self.assertEqual((result["entries"], result["bookings"]), ([], []))
            pin = core.load_conf(str(Path(client["env"]["AGENT_BUS_CONFIG"], "board.conf")))
            self.assertEqual(pin["inode"], str(self.new.stat().st_ino))
            self.assertEqual(pin["fsid"], str(os.statvfs(self.new).f_fsid))
        self.assertEqual(list((self.new / "inbox").iterdir()), [])
        self.assertTrue((self.archive / "inbox" / rid).is_dir())
        self.assertTrue((self.archive / "bookings" / (historical["id"] + ".json")).exists())
        with core.Board(str(self.new)) as new:
            self.assertEqual(new.receipts(), {})
        refusal = self.command(late["bus"], "book", "--gpus", "0", "--from", t + 3600, "--to", t + 7200,
                               "--objection", "1s", env=late["env"], expected=1)
        self.assertIn("over your budget", refusal)
        report("both confirmed clients re-pinned on their next CLI call; the legacy client stayed pinned until upgrade")
        report("history stayed on GPFS; XFS started empty and enforced the prior GPU-hour budget")
        quick = dict(late, info=json.loads(self.command(late["bus"], "run", "--gpus", "0", "--slug", "quick",
                                                       "--detach", "--", "true", env=late["env"])))
        self.clients.append(quick)
        self.wait_for(lambda: self.state(quick).get("state") == "ended", "short job ending on XFS")
        with core.Board(str(self.new)) as board:
            self.assertIn(quick["info"]["id"], {e["id"] for e, _ in move.archive_entries(board) if move.end_record(e)})
        report("a short job on XFS left an archived end record")


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
