"""The read-only page (design 3.11): GET only, bounded threads, a 15 s connection deadline, the last good page kept."""
import http.client, json, os, socket, threading, time, unittest
from pathlib import Path
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
    with patch.object(page, "lab_user", side_effect=lambda user, gid: user):
        return dict(t=now, host="gpuhost", model="Fake GPU", board="/test/board", notices=[], gpus=gpus,
                    entries=page.public_entries(entries, bookings, 0), bookings=page.public_bookings(bookings, now, 0),
                    usage={1: [(now - 120, 30), (now - 60, 95)]})


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


if __name__ == "__main__":
    unittest.main()
