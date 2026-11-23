#!/usr/bin/env python3
"""Unit-level checks of bus.py in a throwaway board: stale-entry removal by the sweep, the one-watch-per-entry lock,
watch exiting on a message and printing an unacked one again, --follow, and the Claude Code stop hook.
Runs on Linux and macOS: `python3 tests/test_bus.py` (a stub nvidia-smi stands in for the GPUs)."""
import getpass, json, os, socket, subprocess, sys, tempfile, time, unittest
from pathlib import Path

BUS = str(Path(__file__).resolve().parent.parent / "bin" / "bus.py")
HOST = socket.gethostname().split(".")[0]


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bus-test-"))
        self.root = self.tmp / "board"
        stub = self.tmp / "stub"
        stub.mkdir()
        (stub / "nvidia-smi").write_text("#!/bin/sh\nexit 0\n")
        (stub / "nvidia-smi").chmod(0o755)
        home = self.tmp / "home"
        home.mkdir()
        self.env = dict(os.environ, AGENT_BUS_ROOT=str(self.root), HOME=str(home),
                        PATH=f"{stub}:{os.environ['PATH']}")
        self.env.pop("CLAUDE_CODE_SESSION_ID", None)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            if p.stdout and not p.stdout.closed:
                p.stdout.close()

    def bus(self, *args, inp=None, env=None, timeout=30):
        return subprocess.run([sys.executable, BUS, *args], input=inp, capture_output=True, text=True,
                              env=env or self.env, timeout=timeout)

    def bg(self, *args, env=None):
        p = subprocess.Popen([sys.executable, BUS, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             env=env or self.env, stdin=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def entry(self, aid):
        return self.root / "agents" / f"{aid}.json"

    def edit(self, aid, **kw):
        p = self.entry(aid)
        a = json.loads(p.read_text())
        a.update(kw)
        p.write_text(json.dumps(a))

    def unwatch(self, aid):
        """Make the entry's heartbeat ten minutes old."""
        old = time.time() - 600
        os.utime(self.root / "agents" / ".heartbeat" / aid, (old, old))
        self.edit(aid, heartbeat=int(old))

    def log(self):
        return [json.loads(l) for l in (self.root / "log.jsonl").read_text().splitlines()]

    def dead_pid(self):
        p = subprocess.Popen(["true"])
        p.wait()
        return p.pid

    # ---- the sweep's stale entries ----

    def test_sweep_removes_stale_entries_only(self):
        live = subprocess.Popen(["sleep", "60"])
        self.procs.append(live)
        self.bus("up", "alice-deadpid", "--gpus", "1", "--task", "t", "--hours", "2", "--pid", str(self.dead_pid()))
        self.bus("up", "alice-livepid-expired", "--gpus", "2", "--task", "t", "--hours", "1", "--pid", str(live.pid))
        self.bus("up", "alice-nopid-expired", "--gpus", "3", "--task", "t", "--hours", "1")
        self.bus("up", "alice-watched-expired", "--gpus", "4", "--task", "t", "--hours", "1")
        self.bus("up", "alice-fresh", "--gpus", "5", "--task", "t", "--hours", "2")
        self.bus("up", "alice-otherhost", "--gpus", "6", "--task", "t", "--hours", "2", "--pid", "1",
                 "--host", "some-other-node")
        self.bus("up", "bob-deadpid", "--gpus", "7", "--task", "t", "--hours", "2", "--pid", str(self.dead_pid()))
        self.edit("bob-deadpid", user="bob")  # another member's entry: the sweep takes it down too
        for aid in ("alice-livepid-expired", "alice-nopid-expired", "alice-watched-expired"):
            self.edit(aid, until=int(time.time()) - 60)
        for aid in ("alice-livepid-expired", "alice-nopid-expired"):
            self.unwatch(aid)
        r = self.bus("sweep")
        self.assertEqual(r.returncode, 0, r.stderr)
        left = sorted(p.stem for p in (self.root / "agents").glob("*.json"))
        self.assertEqual(left, ["alice-fresh", "alice-livepid-expired", "alice-otherhost", "alice-watched-expired"])
        downs = {e["agent"]: e for e in self.log() if e["action"] == "down"}
        self.assertEqual(sorted(downs), ["alice-deadpid", "alice-nopid-expired", "bob-deadpid"])
        self.assertIn("not running on", downs["alice-deadpid"]["reason"])
        self.assertIn("expired", downs["alice-nopid-expired"]["reason"])
        self.assertEqual(downs["bob-deadpid"]["owner"], "bob")
        self.assertTrue(all(e["by"] == "sweep" for e in downs.values()))
        archived = [json.loads(p.read_text()) for p in (self.root / "archive").glob("bob-deadpid-*.json")]
        self.assertEqual(len(archived), 1)
        self.assertIn("stale", archived[0]["down_reason"])
        self.assertTrue((self.root / ".sweep" / f"{getpass.getuser()}@{HOST}").exists())

    # ---- watch ----

    def test_watch_exits_on_message_and_redelivers_until_acked(self):
        self.bus("up", "alice-job", "--gpus", "1", "--task", "t", "--hours", "1")
        w = self.bg("watch", "alice-job", "--interval", "1")
        time.sleep(2)
        self.assertIsNone(w.poll(), "watch exited with nothing to report")
        sent = self.bus("send", "alice-job", "need 2 gpus", "--from", "bob-req", "--kind", "request", "--gpus", "2")
        mid = sent.stdout.split()[1]
        out, _ = w.communicate(timeout=15)
        self.assertEqual(w.returncode, 0)
        self.assertIn(f"MSG {mid}", out)
        self.assertIn("kind=request", out)
        # not acked: the next watch prints it again at once
        r = self.bus("watch", "alice-job", "--interval", "1", timeout=15)
        self.assertIn(f"MSG {mid}", r.stdout)
        self.bus("ack", "alice-job", mid)
        w2 = self.bg("watch", "alice-job", "--interval", "1")
        time.sleep(3)
        self.assertIsNone(w2.poll(), "an acked message was printed again")

    def test_one_watch_per_entry(self):
        self.bus("up", "alice-job", "--gpus", "1", "--task", "t", "--hours", "1")
        w = self.bg("watch", "alice-job", "--interval", "1")
        time.sleep(2)
        r = self.bus("watch", "alice-job", "--interval", "1", timeout=15)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("already watched by", r.stdout)
        self.assertIn(str(w.pid), r.stdout)
        w.kill()
        w.wait()
        w3 = self.bg("watch", "alice-job", "--interval", "1")  # the lock goes with the process
        time.sleep(2)
        self.assertIsNone(w3.poll())

    def test_follow_keeps_printing(self):
        self.bus("up", "alice-job", "--gpus", "1", "--task", "t", "--hours", "1")
        w = self.bg("watch", "alice-job", "--interval", "1", "--follow")
        time.sleep(1.5)
        self.bus("send", "alice-job", "first", "--from", "bob")
        time.sleep(2.5)
        self.bus("send", "alice-job", "second", "--from", "bob")
        time.sleep(2.5)
        self.assertIsNone(w.poll(), "--follow exited")
        w.kill()
        out = w.communicate()[0]
        self.assertIn("WATCH alice-job up", out)
        self.assertIn(": first", out)
        self.assertIn(": second", out)

    def test_watch_ends_when_entry_goes_down(self):
        self.bus("up", "alice-job", "--gpus", "1", "--task", "t", "--hours", "1")
        w = self.bg("watch", "alice-job", "--interval", "1")
        time.sleep(1.5)
        self.bus("down", "alice-job")
        out, _ = w.communicate(timeout=10)
        self.assertIn("WATCH alice-job down", out)

    # ---- stop hook ----

    def hook(self, sid, active=False):
        r = self.bus("stop-hook", inp=json.dumps({"session_id": sid, "stop_hook_active": active,
                                                   "hook_event_name": "Stop"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_stop_hook_blocks_without_watch_and_allows_with_one(self):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID="sess-1")
        self.bus("up", "alice-job", "--gpus", "1", "--task", "t", "--hours", "1", env=env)
        self.assertEqual(json.loads(self.entry("alice-job").read_text())["session"], "sess-1")
        d = self.hook("sess-1")
        self.assertEqual(d["decision"], "block")
        self.assertIn("bus.py watch alice-job", d["reason"])
        self.assertIsNone(self.hook("sess-other"))  # another session's turn is never blocked
        w = self.bg("watch", "alice-job", "--interval", "1")
        time.sleep(2)
        self.assertIsNone(self.hook("sess-1"))
        w.kill()
        w.wait()
        self.assertEqual(self.hook("sess-1")["decision"], "block")
        self.bus("down", "alice-job")
        self.assertIsNone(self.hook("sess-1"))  # nothing on the board: nothing to watch

    def test_stop_hook_caps_blocks_in_a_turn(self):
        self.bus("up", "alice-job", "--gpus", "1", "--task", "t", "--hours", "1", "--session", "sess-2")
        got = [self.hook("sess-2", active=i > 0) for i in range(5)]
        self.assertEqual([bool(g) for g in got], [True, True, True, False, False])
        self.assertTrue(self.hook("sess-2", active=False))  # a new turn starts the count again

    # ---- bookings ----

    def bookings(self):
        return sorted(json.loads(p.read_text())["id"] for p in (self.root / "bookings").glob("*.json"))

    def test_book_overlap_unbook(self):
        r = self.bus("book", "--gpus", "0-3", "--from", "2031-05-01 22:00", "--to", "2031-05-02T01:00",
                     "--note", "timing run", "--as", "alice")
        self.assertEqual(r.returncode, 0, r.stderr)
        first = r.stdout.split()[1].rstrip(":")
        b = json.loads((self.root / "bookings" / f"{first}.json").read_text())
        self.assertEqual((b["gpus"], b["end"] - b["start"], b["name"], b["note"]), ([0, 1, 2, 3], 3 * 3600, "alice", "timing run"))
        for k in ("user", "start", "end", "created"):
            self.assertIn(k, b)
        # overlap on GPU 3: booked anyway, with a warning naming who
        r = self.bus("book", "--gpus", "3,5", "--from", "2031-05-01 23:00", "--to", "2031-05-01 23:30", "--as", "bob")
        self.assertEqual(r.returncode, 0)
        self.assertIn(f"warning: overlaps {first} by alice", r.stdout)
        # no overlap: other GPUs, or the same GPUs right after
        r = self.bus("book", "--gpus", "4", "--from", "2031-05-01 22:00", "--to", "2031-05-01 23:00")
        self.assertNotIn("warning", r.stdout)
        r = self.bus("book", "--gpus", "0", "--from", "2031-05-02 01:00", "--to", "2031-05-02 02:00")
        self.assertNotIn("warning", r.stdout)
        self.assertEqual(len(self.bookings()), 4)
        # end <= start is refused; a bare clock time before --from is the next day
        r = self.bus("book", "--gpus", "1", "--from", "2031-05-01 10:00", "--to", "2031-05-01 09:00")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("before it starts", r.stderr)
        r = self.bus("book", "--gpus", "1", "--from", "2031-05-01 10:00", "--to", "2031-05-01 10:00")
        self.assertNotEqual(r.returncode, 0)
        r = self.bus("book", "--gpus", "9", "--from", "22:00", "--to", "23:00")
        self.assertNotEqual(r.returncode, 0)
        r = self.bus("book", "--gpus", "1", "--from", "tomorrow 22:00", "--to", "01:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        late = json.loads((self.root / "bookings" / f"{r.stdout.split()[1].rstrip(':')}.json").read_text())
        self.assertEqual(late["end"] - late["start"], 3 * 3600)
        self.assertEqual(len(self.bookings()), 5)
        # only the owner removes one
        self.edit_booking(first, user="carol")
        self.assertNotEqual(self.bus("unbook", first).returncode, 0)
        self.edit_booking(first, user=getpass.getuser())
        self.assertEqual(self.bus("unbook", first).returncode, 0)
        self.assertNotIn(first, self.bookings())
        self.assertIn("unbook", [e["action"] for e in self.log()])

    def edit_booking(self, bid, **kw):
        p = self.root / "bookings" / f"{bid}.json"
        p.write_text(json.dumps(dict(json.loads(p.read_text()), **kw)))

    def test_cal_and_ls_show_bookings_and_entries(self):
        (self.root).mkdir(parents=True, exist_ok=True)
        (self.root / "config").write_text(f"host={HOST}\n")
        self.bus("up", "alice-train", "--gpus", "6,7", "--task", "t", "--hours", "3")
        r = self.bus("book", "--gpus", "0-3", "--from", "now", "--to", time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 7200)),
                     "--note", "kernel timing", "--as", "alice")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.bus("book", "--gpus", "2", "--from", "tomorrow 09:00", "--to", "tomorrow 10:00", "--as", "bob")
        self.bus("book", "--gpus", "5", "--from", "2031-01-01 09:00", "--to", "2031-01-01 10:00", "--as", "dave")  # outside
        env = dict(self.env, TZ="America/New_York")
        out = self.bus("cal", env=env).stdout
        rows = {l.split()[0]: l.split()[1] for l in out.splitlines() if l.startswith("gpu")}
        self.assertEqual(sorted(rows), [f"gpu{g}" for g in range(8)])
        self.assertEqual(rows["gpu0"][0], "A")
        self.assertEqual(rows["gpu6"][0], "a")
        self.assertTrue(set(rows["gpu5"]) == {"."})
        self.assertIn("B", rows["gpu2"])
        self.assertIn("kernel timing", out)
        self.assertNotIn("dave", out)
        j = json.loads(self.bus("cal", "--json", "--days", "1").stdout)
        self.assertEqual(j["gpus"], list(range(8)))
        self.assertEqual(j["bookings"][0]["name"], "alice")  # bob's starts tomorrow 09:00, inside or past this day
        self.assertNotIn("dave", [b["name"] for b in j["bookings"]])
        self.assertEqual([(e["id"], e["gpus"]) for e in j["entries"]], [("alice-train", [6, 7])])
        ls = self.bus("ls").stdout
        self.assertIn("bookings, next 24 h", ls)
        self.assertIn("kernel timing", ls)
        self.assertNotIn("dave", ls)

    def test_sweep_drops_ended_bookings(self):
        self.bus("book", "--gpus", "0", "--from", "2031-01-01 09:00", "--to", "2031-01-01 10:00", "--as", "alice")
        r = self.bus("book", "--gpus", "1", "--from", "2031-01-01 09:00", "--to", "2031-01-01 10:00", "--as", "bob")
        ended = r.stdout.split()[1].rstrip(":")
        self.edit_booking(ended, start=int(time.time()) - 7200, end=int(time.time()) - 60, user="bob")
        self.assertEqual(len(self.bookings()), 2)
        self.assertEqual(self.bus("sweep").returncode, 0)
        self.assertEqual(len(self.bookings()), 1)
        self.assertNotIn(ended, self.bookings())
        e = [e for e in self.log() if e["action"] == "unbook"][-1]
        self.assertEqual((e["booking"], e["reason"], e["owner"]), (ended, "ended", "bob"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
