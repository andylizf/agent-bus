"""Shared fixture for the agent-bus v2 tests: a throwaway v2 board (setup-board.sh), a private state, config, bin and
builds directory per test, a fake nvidia-smi, and test-only unit names (prefix abt<hex>) apart from real ones.

Where the board goes: $AGENT_BUS_TEST_ROOT (a directory on the shared filesystem on the GPU host), else a temporary
directory. Systemd tests skip when no user manager is reachable; set AGENT_BUS_TEST_REQUIRE_SYSTEMD=1 to fail instead."""
import json, os, secrets, shutil, signal, subprocess, sys, tempfile, time, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))
BUS = str(REPO / "bin" / "bus")
FAKE_SMI = str(Path(__file__).resolve().parent / "fake_nvidia_smi.py")
PY = sys.executable


def have_systemd():
    try:
        r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() in ("running", "degraded")
    except (OSError, subprocess.TimeoutExpired):
        return False


SYSTEMD = have_systemd()
LINUX = sys.platform.startswith("linux")


def need_systemd(cls):
    if not SYSTEMD and not os.environ.get("AGENT_BUS_TEST_REQUIRE_SYSTEMD"):
        return unittest.skip("no systemd user manager here")(cls)
    return cls


def need_linux(f):
    return f if LINUX else unittest.skip("needs Linux /proc")(f)


SCALE = float(os.environ.get("AGENT_BUS_TEST_TIME_SCALE") or 1)   # a loaded host: every wait in the tests is longer


def wait_for(pred, timeout=30.0, step=0.2, what="condition"):
    timeout *= SCALE
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")


class BusTest(unittest.TestCase):
    """setUp builds a board and an install; self.bus(...) runs the CLI as a subprocess with the test's environment."""
    gpus = 8
    blocks = 3
    board_settings = dict(min_objection_s=1, default_objection_s=2, lapse_s=1800)
    pass_s = 0.5

    def setUp(self):
        base = os.environ.get("AGENT_BUS_TEST_ROOT")
        if base:
            os.makedirs(base, exist_ok=True)
            self.tmp = Path(tempfile.mkdtemp(prefix="t-", dir=base)).resolve()
        else:
            self.tmp = Path(tempfile.mkdtemp(prefix="abt-")).resolve()
        self.addCleanup(self.cleanup)
        self.root = self.tmp / "board"
        r = subprocess.run(["bash", str(REPO / "setup-board.sh"), str(self.root), self.host(), "--gpus", str(self.gpus),
                            "--blocks", str(self.blocks)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg = self.root / "config"
        cfg.write_text(cfg.read_text() + "".join(f"{k}={v}\n" for k, v in self.board_settings.items()))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.prefix = "abt" + secrets.token_hex(3)
        self.gpus_file = self.tmp / "gpus.json"
        self.set_gpus()
        smi = self.tmp / "nvidia-smi"           # a copy with its own mode: the checkout's file modes are not relied on
        shutil.copy(FAKE_SMI, smi)
        smi.chmod(0o755)
        self.env = dict(os.environ, AGENT_BUS_STATE=str(self.home / "state"), AGENT_BUS_CONFIG=str(self.home / "config"),
                        AGENT_BUS_BIN=str(self.home / "bin" / "bus"), AGENT_BUS_BUILDS=str(self.home / "builds"),
                        AGENT_BUS_UNIT_PREFIX=self.prefix, AGENT_BUS_NVIDIA_SMI=str(smi),
                        AGENT_BUS_TEST_GPUS=str(self.gpus_file), AGENT_BUS_PASS_S=str(self.pass_s),
                        AGENT_BUS_TERM_GRACE_S="3", AGENT_BUS_HOOK_TIMEOUT_S="2", AGENT_BUS_HB_STALE_S="120",
                        AGENT_BUS_LEGACY_BOARD=str(self.tmp / "old"))
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.update({k: v for k, v in self.env.items() if k.startswith("AGENT_BUS_")})
        self.addCleanup(self.restore_env)
        r = self.bus("_install", "--src", str(REPO), "--board", str(self.root))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.procs = []

    def restore_env(self):
        for k in [k for k in os.environ if k.startswith("AGENT_BUS_")]:
            del os.environ[k]

    def host(self):
        import socket
        return socket.gethostname().split(".")[0]

    def set_gpus(self, apps=(), fail=False, util=0, n=None):
        n = self.gpus if n is None else n
        self.gpus_file.write_text(json.dumps(dict(
            fail=fail, gpus=[dict(index=i, uuid=f"GPU-{i}", util=util) for i in range(n)],
            apps=[dict(uuid=f"GPU-{g}", pid=p) for g, p in apps])))

    def entry_point(self):
        """The installed `bus` once setUp has installed it (as members run it), else the checkout's."""
        b = self.home / "bin" / "bus" if hasattr(self, "home") else None
        return str(b) if b is not None and b.exists() else BUS

    def bus(self, *args, inp=None, env=None, timeout=60, check=False):
        r = subprocess.run([PY, "-IB", self.entry_point(), *args], input=inp, capture_output=True, text=True,
                           env=env or self.env, timeout=timeout)
        if check:
            self.assertEqual(r.returncode, 0, f"bus {' '.join(args)}: {r.stdout}{r.stderr}")
        return r

    def bus_json(self, *args, **kw):
        r = self.bus(*args, check=True, **kw)
        return json.loads(r.stdout)

    def bg(self, *args, env=None):
        p = subprocess.Popen([PY, "-IB", self.entry_point(), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env or self.env, stdin=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(p)
        return p

    def state(self, rid):
        return json.loads((self.home / "state" / "runs" / f"{rid}.json").read_text())

    def entry_file(self, rid):
        return self.root / "agents" / f"{rid}.json"

    def start(self, cmd, gpus="0", *extra, wait_running=True):
        """bus run --detach; returns the info dict."""
        info = self.tmp / f"info-{secrets.token_hex(3)}.json"
        r = self.bus("run", "--gpus", gpus, "--info-file", str(info), "--detach", *extra, "--", cmd)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        d = json.loads(info.read_text())
        if wait_running:
            wait_for(lambda: self.entry_file(d["id"]).exists(), 30, what="the entry on the board")
        return d

    def wait_state(self, rid, states, timeout=60):
        states = (states,) if isinstance(states, str) else states
        return wait_for(lambda: self.state(rid) if self.state(rid)["state"] in states else None, timeout,
                        what=f"{rid} in {states}")

    def cleanup(self):
        for p in getattr(self, "procs", []):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
            p.wait()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()
        if SYSTEMD and hasattr(self, "prefix"):
            r = subprocess.run(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend",
                                f"{self.prefix}*"], capture_output=True, text=True)
            units = [l.split()[0] for l in r.stdout.splitlines() if l.strip()]
            for u in units:
                if u.endswith(".slice") or u.endswith(".scope") or u.endswith(".service"):
                    subprocess.run(["systemctl", "--user", "kill", "--signal=KILL", u], capture_output=True)
                    subprocess.run(["systemctl", "--user", "stop", u], capture_output=True)
                    subprocess.run(["systemctl", "--user", "reset-failed", u], capture_output=True)
        if os.environ.get("ABT_KEEP"):     # debugging: keep the board and private state
            print(f"\nkept {self.tmp}", file=sys.stderr)
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)


class FakeBoard:
    """Stands in for core.Board in pure booking-state tests: records given directly, no filesystem."""

    def __init__(self, bookings=(), objections=(), receipts=None, **limits):
        from agentbus import core
        self.limits = dict(core.DEFAULT_LIMITS, **limits)
        self.ngpus = self.limits["gpus"]
        self._b, self._o = list(bookings), list(objections)
        self._r = receipts or {}

    def bookings_raw(self):
        return [dict(b) for b in self._b]

    def objections(self):
        return [dict(o) for o in self._o]

    def receipts(self, cache=None):
        return dict(self._r)
