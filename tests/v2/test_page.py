"""The read-only page (design 3.11): GET only, bounded threads, a 15 s connection deadline, the last good page kept."""
import http.client, json, socket, threading, time, unittest

from helpers import BusTest
from agentbus import page


def sample():
    return dict(t=int(time.time()), host="gpuhost", errors=[], identity="ok", ngpus=2,
                entries=[dict(id="alice-20261123-120000-x", user="alice", gpus=[0], state="running", occupancy="live",
                              until=int(time.time()) + 60, kind="training", note="<img src=x onerror=alert(1)>",
                              **{"class": "backfill"})],
                bookings=[dict(id="bob-20261123-120000-b", user="bob", gpus=[1], start=1, end=2, state="objected",
                               objection_close=1, kind="eval", note="", why="",
                               objections=[dict(user="carol", reason="deadline <script>")])],
                gpus=[dict(gpu=0, uuid="G0", util=50, procs=["alice"]), dict(gpu=1, uuid="G1", util=90, procs=["erin"])])


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
        c.request(method, path, body=b"{}" if method != "GET" else None)
        r = c.getresponse()
        return r.status, r.read().decode()

    def test_get_renders_classes_states_objections_old_board_and_off_board_use(self):
        port, _ = self.serve()
        code, body = self.get(port)
        self.assertEqual(code, 200)
        for s in ("backfill", "objected", "carol: deadline", "off-board use: erin"):
            self.assertIn(s, body)

    def test_untrusted_text_escaped(self):
        port, _ = self.serve()
        _, body = self.get(port)
        self.assertNotIn("<script>", body)
        self.assertNotIn("<img", body)
        self.assertIn("&lt;img", body)

    def test_no_post_route(self):
        port, _ = self.serve()
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.get(port, m, "/book")[0], 405)
            self.assertEqual(self.get(port, m, "/")[0], 405)

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
                raise RuntimeError("nvidia-smi hung")
            return sample()
        port, pg = self.serve(collect=flaky)
        pg.refresh()
        code, body = self.get(port)
        self.assertEqual(code, 200)
        self.assertIn("refresh failed", body)
        self.assertIn("alice-20261123-120000-x", body)


class Collect(BusTest):
    def test_collect_reads_board_without_writing(self):
        from agentbus import core
        before = sorted(str(p) for p in self.root.rglob("*"))
        d = page.collect(core.load_conf())
        self.assertEqual(d["identity"], "ok")
        self.assertEqual(len(d["gpus"]), 8)
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
