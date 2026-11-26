"""The read-only page (design 3.11): GET only, bounded threads, a 15 s connection deadline, the last good page kept."""
import http.client, json, os, pwd, re, socket, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import BusTest
from agentbus import page


def sample():
    now = int(time.time())
    bookings = [
        dict(id=f"booking-{i}-hidden", user="bob", gpus=[i], start=now - 1800, end=now + 1800,
             state=state, objection_close=now + 30, active=state == "standing", note="Evaluation",
             objections=[dict(user="carol", reason="Class deadline")] if state == "objected" else [])
        for i, state in enumerate(("standing", "pending", "objected", "lapsed"))
    ]
    entries = [
        dict(id="private-run-id", user="alice", gpus=[1], state="running", occupancy="live",
             started=now - 600, until=now + 3600, note="python /private/work.py", **{"class": "backfill"}),
        dict(user="bob", gpus=[0], state="running", occupancy="live", started=now - 900, until=now + 1800,
             booking="booking-0-hidden", note="Training", **{"class": "scheduled"}),
        dict(user="bob", gpus=[2], state="running", occupancy="unknown", started=now - 900, until=now + 1800,
             booking="booking-2-hidden", note="", **{"class": "scheduled"}),
    ]
    gpus = [dict(gpu=i, util=50, users={user: 23000}, mem_mb=23000, total_mb=100000, available=True)
            for i, user in enumerate(("bob", "alice", "bob", "erin"))]
    with patch.object(page, "lab_user", side_effect=lambda user: user):
        return dict(t=now, host="gpuhost", model="Fake GPU", board="/test/board", notices=[], gpus=gpus,
                    entries=page.public_entries(entries, bookings), bookings=page.public_bookings(bookings, now),
                    usage={1: [(now - 120, 30), (now - 60, 95)]})


def tiles(body):
    return {int(m.group(1)): m.group(0) for m in re.finditer(r'<article class="tile[^"]*" aria-label="GPU (\d+),.*?</article>', body, re.S)}


class Page(unittest.TestCase):
    def serve(self, collect=sample, threads=16, deadline=15.0):
        srv, pg = page.serve(0, "127.0.0.1", collect_fn=collect, threads=threads, deadline=deadline)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        for _ in range(100):
            if pg.html:
                break
            time.sleep(0.05)
        return srv.server_address[1], pg

    def get(self, port, method="GET", path="/"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            c.request(method, path, body=b"{}" if method != "GET" else None)
            r = c.getresponse()
            self.assertEqual(r.getheader("X-Robots-Tag"), page.ROBOTS)
            return r.status, r.read().decode()
        finally:
            c.close()

    def test_get_renders_classes_states_objections_and_off_board_use(self):
        port, _ = self.serve()
        code, body = self.get(port)
        self.assertEqual(code, 200)
        for s in ("Hanken Grotesk", "Scheduled run", "Backfill run", "Scheduled · backfill now",
                  "Standing", "Pending", "Objected", "Lapsed", "carol: Class deadline", "erin", "Not on board",
                  "now-line", "sw-night", "spark-line", "22.5 GiB", "bus book", 'name="robots"',
                  "15 min by default, 10 min minimum", 'class="tag scheduled"', 'class="tag backfill"'):
            self.assertIn(s, body)
        self.assertIn('class="blk scheduled', body)
        self.assertIn('class="blk backfill', body)
        self.assertIn('class="blk book', body)
        self.assertNotIn("<form", body)

    def test_private_notes_and_record_identifiers_are_withheld(self):
        port, _ = self.serve()
        _, body = self.get(port)
        self.assertNotIn("private-run-id", body)
        self.assertNotIn("booking-0-hidden", body)
        self.assertNotIn("/private/work.py", body)
        self.assertIn("[redacted]", body)
        for note in ("<img src=x onerror=alert(1)>", "192.0.2.10", "2001:db8::1", "hf.example.com", "srun train"):
            self.assertEqual(page.public_note(note), "[redacted]")

    def test_telemetry_keeps_unattributed_gpus_out_of_free_count(self):
        data = sample()
        for g, (mem, util) in zip(data["gpus"], ((154624, 0), (0, 1), (0, 0), (2048, None))):
            g.update(users={}, mem_mb=mem, util=util)
        body = page.render(data)
        for gpu in (0, 1):
            self.assertIn(f'aria-label="GPU {gpu}, In use, owner unknown"', body)
        self.assertEqual(body.count('class="state-pill unattributed"'), 2)
        self.assertIn('aria-label="GPU 2, Claimed"', body)
        self.assertIn('aria-label="GPU 3, Free"', body)
        self.assertIn('<b>1</b> of 4 free', body)

    def test_scheduled_tile_names_its_owner_and_window(self):
        t = tiles(page.render(sample()))[0]
        self.assertIn('class="tile busy has-scheduled"', t)
        self.assertIn('<li class="who scheduled">', t)
        self.assertIn('class="task scheduled"', t)
        self.assertRegex(t, r'class="task scheduled"><div class="task-meta"><span class="dot"[^>]*></span><b>bob</b>')
        self.assertRegex(t, r'<span class="until">\d\d:\d\d[^<]*\d\d:\d\d</span>')

    def test_backfill_tile_is_muted_and_does_not_repeat_the_name(self):
        t = tiles(page.render(sample()))[1]
        self.assertIn('class="tile busy backfill-only"', t)
        self.assertIn('<li class="who backfill">', t)
        self.assertIn('class="seg backfill"', t)
        task = t[t.index('class="task backfill"'):]
        self.assertNotIn("alice", task)
        self.assertIn('<span class="tag backfill">Backfill</span>', task)
        self.assertIn("until ", task)
        self.assertNotIn("Not on board", t)

    def test_off_board_use_is_a_warning(self):
        body = page.render(sample())
        t = tiles(body)[3]
        self.assertIn('class="tile offboard"', t)
        self.assertIn('<span class="state-pill offboard">Not on board</span>', t)
        self.assertIn('<li class="who off">', t)
        self.assertIn('<span class="flag">Not on board</span>', t)
        self.assertIn('class="chip alert offboard"><b>1</b> GPU not on board', body)
        self.assertIn('<b>0</b> of 4 free', body)

    def test_in_use_without_an_owner_is_never_free(self):
        data = sample()
        data["entries"] = [e for e in data["entries"] if e["user"] != "alice"]
        data["gpus"][1].update(users={"Unknown user": 150000}, mem_mb=150000, util=100)
        data["gpus"][3].update(users={}, mem_mb=154624, util=1)
        body = page.render(data)
        for gpu in (1, 3):
            t = tiles(body)[gpu]
            self.assertIn(f'aria-label="GPU {gpu}, In use, owner unknown"', t)
            self.assertIn('class="tile unattributed', t)
            self.assertIn('<span class="flag">Owner unknown</span>', t)
            self.assertNotIn("Free", t)
        self.assertIn('class="seg unknown"', tiles(body)[3])
        self.assertIn('class="chip alert unattributed"><b>2</b> GPUs in use, owner unknown', body)
        self.assertIn('<b>0</b> of 4 free', body)
        self.assertIn('class="blk unreg unknown', body)

    def test_public_feed_exposes_only_display_fields(self):
        data = sample()
        data["gpus"][0]["users"]["192.0.2.10"] = 100
        data["gpus"][0]["users"]["Unknown user"] = 200
        data["gpus"][0]["pid"] = 42
        data["entries"][0].update(command="python /private/work.py", note="private-token", id="hidden")
        data["bookings"][0]["id"] = "private-booking"
        feed = page.public_feed(data)
        self.assertEqual(set(feed), {"t", "board", "gpus", "entries", "bookings"})
        self.assertEqual(set(feed["gpus"][0]), {"gpu", "mem_mb", "total_mb", "util", "users", "available"})
        self.assertEqual(set(feed["entries"][0]), {"gpus", "user", "started", "until", "kind"})
        self.assertEqual(set(feed["bookings"][0]), {"gpus", "user", "start", "end", "state", "objection_close"})
        self.assertEqual(feed["board"], "/test/board")
        self.assertEqual(feed["gpus"][0]["users"], {"bob": 23000, "Unknown user": 300})
        self.assertEqual([entry["kind"] for entry in feed["entries"]], ["backfill", "scheduled", "backfill"])
        self.assertEqual(feed["bookings"][0]["state"], "standing")
        for private in ("192.0.2.10", "password", "/private/work.py", "private-token", "private-booking",
                        "Class deadline", "gpuhost", "Fake GPU"):
            self.assertNotIn(private, json.dumps(feed))

    def test_only_get_root_is_served(self):
        port, _ = self.serve()
        for m in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT", "CUSTOM"):
            self.assertEqual(self.get(port, m, "/book")[0], 405)
            self.assertEqual(self.get(port, m, "/")[0], 405)
        for path in ("/book", "/bookings/", "/config", "/.git/config", "/%2e%2e/config"):
            self.assertEqual(self.get(port, "GET", path)[0], 404)

    def test_slow_header_client_cut_at_deadline(self):
        port, _ = self.serve(deadline=1.0)
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")   # never finishes its headers
        t = time.monotonic()
        s.settimeout(10)
        data = s.recv(100)
        self.assertEqual(data, b"")
        self.assertLess(time.monotonic() - t, 5)
        s.close()

    def test_overload_answered_503(self):
        port, _ = self.serve(threads=1, deadline=3.0)
        hold = socket.create_connection(("127.0.0.1", port))
        hold.sendall(b"GET / HTTP/1.1\r\n")
        time.sleep(0.3)
        self.assertEqual(self.get(port)[0], 503)
        hold.close()

    def test_failed_refresh_serves_last_good_page_with_banner(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("/private/nvidia-smi hung")
            return sample()
        port, pg = self.serve(collect=flaky)
        pg.refresh()
        code, body = self.get(port)
        self.assertEqual(code, 200)
        self.assertIn("refresh failed", body)
        self.assertIn("Training", body)
        self.assertNotIn("/private/nvidia-smi", body)


class ProcessOwners(unittest.TestCase):
    def setUp(self):
        page.lab_user.cache_clear()
        self.addCleanup(page.lab_user.cache_clear)

    def test_any_account_is_shown_by_netid_whatever_its_groups(self):
        # A process owner outside the board's unix group is still a person on the node, not "Unknown user".
        fake = pwd.struct_passwd(("abc1234", "x", 60001, 60001, "", "/home/abc1234", "/bin/bash"))
        with patch.object(page.pwd, "getpwnam", return_value=fake), \
             patch.object(page.os, "getgrouplist", return_value=[60001], create=True):
            self.assertEqual(page.lab_user("abc1234"), "abc1234")

    def test_names_that_are_not_netid_accounts_stay_unknown(self):
        with patch.object(page.pwd, "getpwnam", side_effect=KeyError("no such user")):
            self.assertEqual(page.lab_user("zz9999"), "Unknown user")
        for name in (None, "", "root-like name", "192.0.2.10", "Abc1234"):
            self.assertEqual(page.lab_user(name), "Unknown user")

    def gpu_view(self, gpus, compute, report=""):
        rows = {"--query-gpu=index,uuid,pci.bus_id,memory.used,memory.total,utilization.gpu,name": gpus,
                "--query-compute-apps=gpu_uuid,pid,used_gpu_memory": compute}
        fake = pwd.struct_passwd(("abc1234", "x", 60001, 60001, "", "/home/abc1234", "/bin/bash"))
        with patch.object(page, "smi", side_effect=rows.get), \
             patch.object(page.subprocess, "run", return_value=SimpleNamespace(stdout=report)), \
             patch.object(page.core, "proc_uid", return_value=60001), \
             patch.object(page.core, "owner_name", return_value="abc1234"), \
             patch.object(page.pwd, "getpwnam", return_value=fake):
            return page.gpu_view()[1]

    def test_gpu_view_names_a_process_owner_outside_the_board_group(self):
        gpus = self.gpu_view([["7", "GPU-7", "00000000:F0:00.0", "109922", "275040", "100", "NVIDIA B300"]],
                             [["GPU-7", "4242", "109900"]])
        self.assertEqual(gpus[0]["users"], {"abc1234": 109900})

    def test_graphics_only_processes_are_attributed_to_their_owner(self):
        # EGL rendering (MuJoCo, for one) runs as Type G processes, which --query-compute-apps does not list.
        gpus = self.gpu_view([["0", "GPU-0", "00000000:1A:00.0", "22600", "275040", "100", "NVIDIA B300"],
                              ["1", "GPU-1", "00000000:2B:00.0", "80000", "275040", "90", "NVIDIA B300"]],
                             [["GPU-1", "4243", "79000"]], PIDS_REPORT)
        self.assertEqual(gpus[0]["users"], {"abc1234": 2876 + 2508})
        self.assertEqual(gpus[1]["users"], {"abc1234": 79000})  # the C and C+G entries come from the compute query
        body = page.render(dict(sample(), gpus=[dict(gpus[0], total_mb=275040)], entries=[], bookings=[]))
        self.assertIn('aria-label="GPU 0, Not on board"', body)
        self.assertNotIn("owner unknown", body)

    def test_pids_report_parser_keeps_graphics_only_entries(self):
        self.assertEqual(page.graphics_apps(PIDS_REPORT),
                         [("00000000:1A:00.0", 1824987, 2876), ("00000000:1A:00.0", 1825968, 2508)])
        self.assertEqual(page.graphics_apps("No devices were found"), [])


PIDS_REPORT = """
==============NVSMI LOG==============

Timestamp                                              : Thu Nov 26 10:30:56 2026
Attached GPUs                                          : 2
GPU 00000000:1A:00.0
    Processes
        GPU instance ID                   : N/A
        Compute instance ID               : N/A
        Process ID                        : 1824987
            Type                          : G
            Name                          : /scratch/example/.venv/bin/python
            Used GPU Memory               : 2876 MiB
        GPU instance ID                   : N/A
        Compute instance ID               : N/A
        Process ID                        : 1825968
            Type                          : G
            Name                          : /scratch/example/.venv/bin/python
            Used GPU Memory               : 2508 MiB

GPU 00000000:2B:00.0
    Processes
        Process ID                        : 4243
            Type                          : C
            Name                          : python
            Used GPU Memory               : 79000 MiB
        Process ID                        : 4244
            Type                          : C+G
            Name                          : python
            Used GPU Memory               : N/A
"""


class Collect(BusTest):
    def test_collect_reads_board_without_writing(self):
        from agentbus import core
        state = Path(core.STATE())
        before = sorted(str(p) for root in (self.root, state) for p in root.rglob("*"))
        d = page.collect(core.load_conf())
        self.assertEqual(d["board"], str(self.root))
        self.assertEqual(len(d["gpus"]), 8)
        self.assertEqual(sorted(str(p) for root in (self.root, state) for p in root.rglob("*")), before)

    def test_private_utilization_and_process_memory(self):
        from agentbus import core
        now = core.now()
        history = Path(core.private_dir("usage"), "private-name.jsonl")
        history.write_text("\n".join((json.dumps(dict(t=now - 180, util={1: 25})), "partial",
                                      json.dumps(dict(t=now - 60, util={1: 75})))))
        self.set_gpus(apps=[(1, os.getpid())])
        spec = json.loads(self.gpus_file.read_text())
        spec["apps"][0]["mem"] = 16384
        self.gpus_file.write_text(json.dumps(spec))
        data = page.collect(core.load_conf())
        self.assertEqual(list(data["gpus"][1]["users"].values()), [16384])
        self.assertEqual(data["usage"][1], [(now - 180, 25), (now - 60, 75)])
        self.assertNotIn("private-name", page.render(data))

    def test_collect_counts_graphics_only_processes(self):
        from agentbus import core
        self.set_gpus(apps=[(2, os.getpid())])
        spec = json.loads(self.gpus_file.read_text())
        spec["apps"][0].update(mem=2876, type="G")
        self.gpus_file.write_text(json.dumps(spec))
        data = page.collect(core.load_conf())
        self.assertEqual(list(data["gpus"][2]["users"].values()), [2876])
        self.assertEqual(data["notices"], [])


if __name__ == "__main__":
    unittest.main()
