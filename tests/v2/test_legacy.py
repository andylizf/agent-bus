"""The legacy adapter on a test copy of the old board (bff1824 format): confined writes, nothing followed, no chmod,
mkdir only for a new reply inbox, and bff1824's own tools read what it writes."""
import json, os, signal, stat, subprocess, sys, time, unittest

from helpers import BusTest, LINUX, REPO, need_linux, wait_for
from agentbus import core, legacy

OLD_BUS = str(REPO / "legacy" / "bin" / "bus.py")


@unittest.skipUnless(LINUX, "the adapter binds entries to processes through /proc (Linux)")
class Legacy(BusTest):
    def setUp(self):
        super().setUp()
        self.old = self.tmp / "old"
        r = subprocess.run(["bash", str(REPO / "legacy" / "setup-board.sh"), str(self.old), self.host()],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for d in ("agents/.heartbeat", ".sweep"):
            (self.old / d).mkdir(parents=True, exist_ok=True)
            os.chmod(self.old / d, 0o2775)
        self.oldenv = dict(self.env, AGENT_BUS_ROOT=str(self.old))
        self.modes = self.snapshot_modes()

    def snapshot_modes(self):
        return {str(p): stat.S_IMODE(p.lstat().st_mode) for p in self.old.rglob("*") if p.is_dir()}

    def oldbus(self, *args):
        return subprocess.run([sys.executable, OLD_BUS, *args], capture_output=True, text=True, env=self.oldenv,
                              timeout=60)

    def lbus(self, *args):
        return self.bus("legacy", *args[:1], "--board", str(self.old), *args[1:])

    def aid(self, s="task"):
        return f"{core.USER}-20261123-120000-{s}"

    def up(self, aid=None, pid=None, **kw):
        aid = aid or self.aid()
        p = pid or os.getpid()
        r = self.lbus("up", aid, "--gpus", "1,3", "--task", "test task", "--hours", "2", "--pid", str(p),
                      *sum(([f"--{k.replace('_', '-')}", str(v)] for k, v in kw.items()), []))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return aid

    def test_bff1824_ls_reads_adapter_entry(self):
        aid = self.up()
        rows = json.loads(self.oldbus("ls", "--json").stdout)
        self.assertEqual(rows[0]["id"], aid)
        self.assertEqual(rows[0]["gpus"], "1,3")
        self.assertEqual(rows[0]["user"], core.USER)
        self.assertIn(rows[0]["state"], ("live", "exited"))
        for k in ("human", "host", "task", "pid", "until", "on_request", "exclusive", "started", "heartbeat", "seen"):
            self.assertIn(k, rows[0])

    def test_bff1824_inbox_reads_adapter_message_and_adapter_reads_bff1824(self):
        aid = self.up()
        r = self.lbus("send", aid, "need 2 gpus", "--from", "someone", "--kind", "request", "--gpus", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("need 2 gpus", self.oldbus("inbox", aid).stdout)
        self.oldbus("send", aid, "hello from v1", "--from", "carol")
        out = json.loads(self.lbus("inbox", aid, "--json").stdout)
        self.assertEqual({m["text"] for m in out}, {"need 2 gpus", "hello from v1"})

    def test_bff1824_entry_read_by_adapter_ls(self):
        self.oldbus("up", "carol-run", "--gpus", "4", "--task", "v1 task", "--hours", "1")
        rows = json.loads(self.lbus("ls", "--json").stdout)
        self.assertEqual([r["id"] for r in rows], ["carol-run"])
        self.assertIsNone(rows[0]["identity_record"]) if rows[0]["file_owner"] != core.USER else None

    @need_linux
    def test_identity_record_written(self):
        aid = self.up()
        rec = json.loads((self.home / "state" / "legacy" / f"{aid}.json").read_text())
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual(rec["ticks"], core.proc_ticks(os.getpid()))
        self.assertEqual(oct(stat.S_IMODE((self.home / "state" / "legacy" / f"{aid}.json").stat().st_mode)), "0o600")

    def test_symlinked_entry_path_not_followed(self):
        aid = self.aid("sym")
        target = self.tmp / "target.json"
        target.write_text("keep")
        os.symlink(target, self.old / "agents" / f"{aid}.json")
        r = self.lbus("up", aid, "--gpus", "0", "--task", "t", "--hours", "1")
        self.assertEqual(target.read_text(), "keep")
        if r.returncode == 0:
            self.assertFalse(os.path.islink(self.old / "agents" / f"{aid}.json"))

    def test_symlinked_heartbeat_not_followed(self):
        aid = self.up()
        hb = self.old / "agents" / ".heartbeat" / aid
        hb.unlink()
        target = self.tmp / "t"
        target.write_text("")
        old = target.stat().st_mtime
        os.symlink(target, hb)
        time.sleep(0.02)
        r = self.lbus("beat", aid)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(target.stat().st_mtime, old)

    def test_symlinked_inbox_not_followed(self):
        aid = self.up()
        os.rename(self.old / "inbox" / "all", self.tmp / "elsewhere")
        os.symlink(self.tmp / "elsewhere", self.old / "inbox" / "all")
        r = self.lbus("send", "all", "broadcast")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.listdir(self.tmp / "elsewhere"), [])

    def test_no_chmod_and_mkdir_only_for_new_inbox(self):
        aid = self.up()
        self.lbus("send", aid, "x", "--kind", "msg")
        self.lbus("beat", aid)
        self.lbus("ack", aid, "nothing")
        self.lbus("send", "brand-new-box", "hi", "--kind", "reply")
        after = self.snapshot_modes()
        new = {k: v for k, v in after.items() if k not in self.modes}
        self.assertEqual({os.path.relpath(k, self.old) for k in new}, {f"inbox/{aid}", "inbox/brand-new-box"})
        self.assertEqual(set(new.values()), {0o2775})
        self.assertEqual({k: v for k, v in after.items() if k in self.modes}, self.modes)

    def test_down_archives_and_removes_heartbeat(self):
        aid = self.up()
        self.assertEqual(self.lbus("down", aid, "--reason", "finished").returncode, 0)
        self.assertFalse((self.old / "agents" / f"{aid}.json").exists())
        self.assertFalse((self.old / "agents" / ".heartbeat" / aid).exists())
        arch = [json.loads(p.read_text()) for p in (self.old / "archive").glob(f"{aid}-*.json")]
        self.assertEqual(arch[0]["down_reason"], "finished")
        self.assertFalse((self.home / "state" / "legacy" / f"{aid}.json").exists())

    def test_log_appended(self):
        aid = self.up()
        lines = [json.loads(l) for l in (self.old / "log.jsonl").read_text().splitlines()]
        self.assertEqual(lines[-1]["action"], "up")
        self.assertEqual(lines[-1]["agent"], aid)

    def test_old_pin_mismatch_refuses_writes(self):
        conf = core.load_conf()
        st = os.stat(self.old)
        conf.update(legacy_path=str(self.old), legacy_inode=str(st.st_ino + 1), legacy_fsid="1")
        core.save_conf(conf)
        r = self.lbus("up", self.aid(), "--gpus", "0", "--task", "t", "--hours", "1")
        self.assertIn("identity changed", r.stderr)

    def make_request(self, aid, age=0):
        if age:   # the entry must predate the request, or bff1824's unread filter drops it
            p = self.old / "agents" / f"{aid}.json"
            a = json.loads(p.read_text())
            a["started"] -= age + 100
            os.unlink(p)
            p.write_text(json.dumps(a))
        self.oldbus("send", aid, "please free", "--from", "carol-req", "--kind", "request")
        for p in (self.old / "inbox" / aid).glob("*.json"):
            m = json.loads(p.read_text())
            m["ts"] -= age
            p.write_text(json.dumps(m))

    def unwatch(self, aid):
        old = time.time() - 600
        os.utime(self.old / "agents" / ".heartbeat" / aid, (old, old))
        p = self.old / "agents" / f"{aid}.json"
        a = json.loads(p.read_text())
        a["heartbeat"] = int(old)
        os.unlink(p)
        p.write_text(json.dumps(a))

    def test_sweep_own_without_record_is_advisory(self):
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(sleeper.kill)
        aid = self.aid("adv")
        self.oldbus("up", aid, "--gpus", "1", "--task", "pre-cutover", "--hours", "2", "--pid", str(sleeper.pid))
        hb = self.old / "agents" / ".heartbeat" / aid
        self.make_request(aid, age=400)
        os.utime(hb, None)
        r = self.lbus("sweep-own", "--reply", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["stopped"], [])
        self.assertEqual(rep["advisory"][0]["entry"], aid)
        self.assertIsNone(sleeper.poll())
        again = json.loads(self.lbus("sweep-own", "--json").stdout)
        self.assertEqual(again["advisory"], [])       # reported once
        lines = (self.home / "state" / "legacy" / "unanswered.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)

    @need_linux
    def test_sweep_own_signals_through_matching_record(self):
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(sleeper.kill)
        aid = self.up(pid=sleeper.pid)
        self.unwatch(aid)
        os.utime(self.old / "agents" / ".heartbeat" / aid, None)  # fresh file mtime: not stale, but unattended JSON
        self.make_request(aid)
        self.unwatch(aid)
        rec = json.loads((self.home / "state" / "legacy" / f"{aid}.json").read_text())
        self.assertEqual(rec["pid"], sleeper.pid)
        r = json.loads(self.lbus("sweep-own", "--reply", "--json").stdout)
        self.assertEqual([x["entry"] for x in r["stopped"]], [aid], r)
        self.assertEqual(sleeper.wait(10), -signal.SIGTERM)
        self.assertIn("was stopped", self.oldbus("inbox", "carol-req", "--all").stdout)

    @need_linux
    def test_sweep_own_mismatched_record_not_signalled(self):
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(sleeper.kill)
        aid = self.up(pid=sleeper.pid)
        p = self.home / "state" / "legacy" / f"{aid}.json"
        rec = json.loads(p.read_text())
        rec["ticks"] += 1     # the pid now names a different process than the one recorded
        p.write_text(json.dumps(rec))
        self.make_request(aid)
        self.unwatch(aid)
        os.utime(self.old / "agents" / ".heartbeat" / aid, None)
        r = json.loads(self.lbus("sweep-own", "--json").stdout)
        self.assertEqual(r["stopped"], [])
        self.assertIsNone(sleeper.poll())

    def test_sweep_own_archives_stale_own_entry_without_live_record(self):
        aid = self.aid("stale")
        self.oldbus("up", aid, "--gpus", "1", "--task", "gone", "--hours", "1")
        old = time.time() - 200
        os.utime(self.old / "agents" / ".heartbeat" / aid, (old, old))
        r = json.loads(self.lbus("sweep-own", "--json").stdout)
        self.assertEqual(r["archived"], [aid])
        self.assertFalse((self.old / "agents" / f"{aid}.json").exists())

    def test_sweep_own_keeps_fresh_heartbeat(self):
        aid = self.aid("fresh")
        self.oldbus("up", aid, "--gpus", "1", "--task", "alive", "--hours", "1")
        r = json.loads(self.lbus("sweep-own", "--json").stdout)
        self.assertEqual(r["archived"], [])

    def test_sweep_own_drops_own_ended_bookings_only(self):
        t = int(time.time())
        self.oldbus("book", "--gpus", "0", "--from", "now", "--to", time.strftime("%Y-%m-%d %H:%M",
                                                                              time.localtime(t + 120)))
        p = next((self.old / "bookings").glob("*.json"))
        b = json.loads(p.read_text())
        b["end"] = t - 10
        p.write_text(json.dumps(b))
        self.lbus("sweep-own")
        self.assertFalse(p.exists())

    def test_cal_json(self):
        self.oldbus("book", "--gpus", "2", "--from", "tomorrow 09:00", "--to", "tomorrow 10:00", "--note", "eval")
        d = json.loads(self.lbus("cal", "--json").stdout)
        self.assertEqual(d["bookings"][0]["note"], "eval")


if __name__ == "__main__":
    unittest.main()
