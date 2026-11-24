"""Readers and confined file operations (design 2.1, 3.2): what a member can plant on the board is not followed,
not trusted, and not acted on."""
import json, os, stat, subprocess, sys, time, unittest

from helpers import BusTest, LINUX, need_linux
from agentbus import core


def entry(user=None, **kw):
    user = user or core.USER
    e = dict(id=f"{user}-20261123-120000-x", user=user, host="h", gpus=[0], booking=None, run="a" * 32,
             started=1, until=2, kind="other", exclusive=False, note="", state="running", schema=2)
    e["class"] = "backfill"
    e.update(kw)
    return e


class Readers(BusTest):
    def put(self, sub, name, obj):
        p = self.root / sub / name
        p.write_text(json.dumps(obj))
        return p

    def entries(self):
        with core.Board(str(self.root)) as b:
            return b.entries(), b.warnings

    def test_valid_entry_read(self):
        e = entry()
        self.put("agents", f"{e['id']}.json", e)
        self.assertEqual([x["id"] for x in self.entries()[0]], [e["id"]])

    def test_symlink_record_skipped(self):
        e = entry()
        real = self.put("archive", "real.json", e)
        os.symlink(real, self.root / "agents" / f"{e['id']}.json")
        self.assertEqual(self.entries()[0], [])

    def test_fifo_skipped_without_blocking(self):
        os.mkfifo(self.root / "agents" / f"{core.USER}-20261123-120000-f.json")
        t = time.monotonic()
        self.assertEqual(self.entries()[0], [])
        self.assertLess(time.monotonic() - t, 5)

    def test_file_over_64_kib_skipped(self):
        e = entry(note="")
        p = self.put("agents", f"{e['id']}.json", e)
        p.write_text(json.dumps(e) + " " * (65 * 1024))
        self.assertEqual(self.entries()[0], [])

    def test_more_than_20000_entries_reported_and_skipped(self):
        d = self.root / "agents"
        for i in range(20002):
            (d / f"x{i}.json").touch()
        es, warn = self.entries()
        self.assertEqual(es, [])
        self.assertTrue(any("more than 20000" in w for w in warn), warn)

    def test_owner_must_equal_user_field(self):
        e = entry(user="bob")
        self.put("agents", f"{e['id']}.json", e)
        self.assertEqual(self.entries()[0], [])

    def test_stem_must_equal_id(self):
        e = entry()
        self.put("agents", f"{core.USER}-20261123-120000-other.json", e)
        self.assertEqual(self.entries()[0], [])

    def test_id_must_start_with_user(self):
        e = entry(id="someone-20261123-120000-x")
        self.put("agents", "someone-20261123-120000-x.json", e)
        self.assertEqual(self.entries()[0], [])

    def test_bad_shapes_rejected(self):
        for bad in (dict(gpus=[9]), dict(gpus=[]), dict(gpus=[0, 0]), dict(state="weird"), dict(run="xyz"),
                    dict(schema=1), dict(note="x" * 81), {"class": "vip"}, dict(kind="research")):
            self.assertFalse(core.check_entry(entry(**bad), 8), bad)
        self.assertTrue(core.check_entry(entry(), 8))

    def test_dot_names_skipped(self):
        e = entry()
        self.put("agents", f".{e['id']}.json", e)
        self.assertEqual(self.entries()[0], [])

    def test_heartbeat_symlink_does_not_count(self):
        e = entry()
        self.put("agents", f"{e['id']}.json", e)
        (self.tmp / "hb").touch()
        os.symlink(self.tmp / "hb", self.root / "agents" / ".heartbeat" / e["id"])
        with core.Board(str(self.root)) as b:
            self.assertEqual(b.occupancy(b.entries()[0]), "unknown")

    def test_stale_heartbeat_is_occupancy_unknown(self):
        e = entry()
        self.put("agents", f"{e['id']}.json", e)
        hb = self.root / "agents" / ".heartbeat" / e["id"]
        hb.touch()
        with core.Board(str(self.root)) as b:
            self.assertEqual(b.occupancy(b.entries()[0]), "live")
        os.utime(hb, (time.time() - 121, time.time() - 121))
        with core.Board(str(self.root)) as b:
            self.assertEqual(b.occupancy(b.entries()[0]), "unknown")

    def test_message_shapes(self):
        m = dict(id=f"{core.USER}-20261123-120000-m", sender=core.USER, to="x", kind="request",
                 request_id="b" * 32, reply_to=f"{core.USER}-mail", to_run="a" * 32, note="", schema=2)
        self.assertTrue(core.check_message(m))
        self.assertFalse(core.check_message(dict(m, request_id="short")))
        self.assertFalse(core.check_message(dict(m, reply_to="bob-mail")))
        self.assertFalse(core.check_message(dict(m, kind="reply", answer="no", reason="")))
        self.assertTrue(core.check_message(dict(m, kind="reply", answer="no", reason="still training")))

    def test_board_config_limits_parsed_not_executed(self):
        with open(self.root / "config", "a") as f:
            f.write("budget_gpu_h=10\n$(touch /tmp/never)=1\n")
        with core.Board(str(self.root)) as b:
            self.assertEqual(b.limits["budget_gpu_h"], 10.0)


class Confined(BusTest):
    def fd(self, *parts):
        return core.walk(str(self.root.joinpath(*parts)))

    def test_walk_refuses_symlinked_component(self):
        os.symlink(self.root, self.tmp / "link")
        with self.assertRaises(OSError):
            core.walk(str(self.tmp / "link" / "agents"))

    def test_publish_is_once(self):
        fd = self.fd("bookings")
        core.publish(fd, "a.json", b"{}")
        with self.assertRaises(FileExistsError):
            core.publish(fd, "a.json", b"{}")
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "bookings" / "a.json").st_mode), 0o644)
        self.assertEqual([n for n in os.listdir(self.root / "bookings") if n.startswith(".tmp")], [])

    def test_replace_does_not_follow_symlink(self):
        target = self.tmp / "target"
        target.write_text("keep")
        os.symlink(target, self.root / "agents" / "x.json")
        core.replace(self.fd("agents"), "x.json", b"new")
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse(os.path.islink(self.root / "agents" / "x.json"))

    def test_touch_refuses_symlink(self):
        target = self.tmp / "target"
        target.write_text("keep")
        old = target.stat().st_mtime
        os.symlink(target, self.root / "agents" / ".heartbeat" / "x")
        time.sleep(0.01)
        self.assertFalse(core.touch(self.fd("agents", ".heartbeat"), "x"))
        self.assertEqual(target.stat().st_mtime, old)

    def test_append_refuses_symlink(self):
        target = self.tmp / "target"
        target.write_text("")
        os.symlink(target, self.root / "log" / "me.jsonl")
        with self.assertRaises(OSError):
            core.append(self.fd("log"), "me.jsonl", "x")
        self.assertEqual(target.read_text(), "")

    def test_mkdir_own_refuses_non_directory(self):
        (self.root / "inbox" / "taken").write_text("")
        self.assertIsNone(core.mkdir_own(self.fd("inbox"), "taken", 0o3775))
        fd = core.mkdir_own(self.fd("inbox"), "mine", 0o3775)
        self.assertIsNotNone(fd)
        self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o3775)

    def test_board_dir_symlinked_subdir_is_missing(self):
        os.rename(self.root / "bookings", self.tmp / "bk")
        os.symlink(self.tmp / "bk", self.root / "bookings")
        with core.Board(str(self.root)) as b:
            self.assertIsNone(b.dir("bookings"))
            self.assertEqual(b.bookings_raw(), [])

    def test_pin_mismatch_reads_but_refuses_writes(self):
        conf = core.load_conf()
        os.rename(self.root, self.tmp / "aside")
        r = subprocess.run(["bash", str(__import__("helpers").REPO / "setup-board.sh"), str(self.root), self.host(),
                            "--blocks", "1"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        with core.Board.from_conf(conf) as b:
            self.assertFalse(b.ok)
            with self.assertRaises(core.Refused):
                b.writable()
        t = int(time.time())
        r = self.bus("book", "--gpus", "0", "--from", str(t + 3600), "--to", str(t + 7200))
        self.assertIn("identity changed", r.stderr)
        self.assertEqual(os.listdir(self.root / "bookings"), [])
        self.assertIn('"identity": "changed"', self.bus("ls", "--json").stdout)

    def test_unit_names_have_no_hyphen_nesting(self):
        self.assertNotIn("-", core.unit_name("alice-20261123-120000-train")[len(core.PREFIX()) + 1:])

    def test_parse_gpus(self):
        self.assertEqual(core.parse_gpus("0-2,5"), [0, 1, 2, 5])
        for bad in ("8", "3-1", "x", ""):
            with self.assertRaises(core.Refused):
                core.parse_gpus(bad)

    @need_linux
    def test_proc_identity(self):
        ident = core.proc_identity(os.getpid())
        self.assertTrue(core.identity_alive(ident))
        self.assertFalse(core.identity_alive(dict(ident, ticks=ident["ticks"] + 1)))
        self.assertFalse(core.identity_alive(dict(ident, boot_id="other")))


class Setup(BusTest):
    def test_layout_and_modes(self):
        modes = {d: stat.S_IMODE(os.stat(self.root / d).st_mode) for d in
                 ("agents", "agents/.heartbeat", "inbox", "bookings", "objections", "seq", "seq/0", "archive", "log")}
        self.assertEqual(set(modes.values()), {0o3775}, modes)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o2755)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "config").st_mode), 0o644)
        self.assertFalse(any(p.suffix in (".py", ".sh") for p in self.root.rglob("*")), "no executable on the board")

    def test_add_blocks(self):
        r = subprocess.run(["bash", str(__import__("helpers").REPO / "setup-board.sh"), str(self.root), "--add-blocks",
                            "2"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.root / "seq" / "4").is_dir())

    def test_refuses_old_board_directory(self):
        old = self.tmp / "oldb"
        (old / "bin").mkdir(parents=True)
        (old / "bin" / "bus.py").write_text("")
        r = subprocess.run(["bash", str(__import__("helpers").REPO / "setup-board.sh"), str(old), "h"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("old (v1) board", r.stderr)

    def test_install_records_pin_and_build(self):
        conf = core.load_conf()
        st = os.stat(self.root)
        self.assertEqual(int(conf["inode"]), st.st_ino)
        self.assertEqual(conf["path"], str(self.root))
        link = os.path.realpath(self.home / "bin" / "bus")
        self.assertTrue(link.startswith(str(self.home / "builds")), link)
        self.assertFalse(os.stat(os.path.dirname(os.path.dirname(link))).st_mode & 0o022)

    def test_install_sh_writes_adopt_units(self):
        env = dict(self.env, XDG_CONFIG_HOME=str(self.home / "xdg"), AGENT_BUS_TIMER="abt-adopt")
        r = subprocess.run(["bash", str(__import__("helpers").REPO / "bin" / "install.sh"), "--board", str(self.root)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        svc = (self.home / "xdg" / "systemd" / "user" / "abt-adopt.service").read_text()
        tim = (self.home / "xdg" / "systemd" / "user" / "abt-adopt.timer").read_text()
        self.assertIn("adopt --all", svc)
        self.assertIn("ConditionHost=", tim)
        self.assertIn("OnUnitActiveSec=30", tim)
        self.assertIn("Environment=AGENT_BUS_STATE=", svc)


if __name__ == "__main__":
    unittest.main()
