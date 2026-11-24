#!/usr/bin/env python3
"""Checks tools/board_server.py in a throwaway board: input validation, POST /book and /unbook running the real
bus.py (overlap warning, the name check on removal, bookings made from a terminal), the write log, the methods and
content types refused, and that it listens on 127.0.0.1. `python3 tests/test_board_server.py`."""
import getpass, json, shutil, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "tests"))
import board_page, board_server  # noqa: E402
from test_board_page import fixture  # noqa: E402

TZ = board_page.TZ


def wall(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")


def tomorrow(h, m=0):
    d = datetime.now(TZ) + timedelta(days=1)
    return d.replace(hour=h, minute=m, second=0, microsecond=0)


class Server(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="board-server-test-"))
        (self.root / "bin").mkdir()
        shutil.copy(REPO / "bin" / "bus.py", self.root / "bin" / "bus.py")
        (self.root / "config").write_text("host=gpubox\n")

        def collect():
            d = fixture()
            d["now"] = int(time.time())
            d["bookings"] = [json.loads(p.read_text()) for p in sorted((self.root / "bookings").glob("*.json"))]
            return d
        self.httpd = board_server.serve(self.root, 0, collect)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.root)

    def req(self, path, body=None, method=None, ctype="application/json"):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            r.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def book(self, **kw):
        body = dict(name="alice", gpus=[0], from_=wall(tomorrow(3)), to=wall(tomorrow(4)), note="")
        body.update(kw)
        body["from"] = body.pop("from_")
        code, text = self.req("/book", body)
        return code, json.loads(text)

    def bookings(self):
        return [json.loads(p.read_text()) for p in (self.root / "bookings").glob("*.json")]

    def weblog(self):
        p = self.root / "bookings-web.log"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_listens_on_loopback_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_page_has_form_and_tiles(self):
        code, page = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn('id="bookform"', page)
        self.assertEqual(page.count('class="tile '), 8)
        self.assertIn('data-t0="', page)

    def test_book_then_unbook(self):
        code, r = self.book(name="alice", gpus=[0, 1], note="kernel timing -x")
        self.assertEqual(code, 200, r)
        bid = r["id"]
        [b] = self.bookings()
        self.assertEqual((b["id"], b["name"], b["gpus"], b["note"]), (bid, "alice", [0, 1], "kernel timing -x"))
        self.assertEqual(b["user"], getpass.getuser())
        self.assertIn('data-id="%s"' % bid, self.req("/")[1])  # the cache was dropped, and Remove shows
        code, r = self.req("/unbook", dict(id=bid, name="bob"))
        self.assertEqual(code, 403)
        self.assertEqual(len(self.bookings()), 1)
        code, r = self.req("/unbook", dict(id=bid, name="Alice"))  # case does not matter
        self.assertEqual(code, 200, r)
        self.assertEqual(self.bookings(), [])
        log = self.weblog()
        self.assertEqual([(l["action"], l["result"][:7]) for l in log], [("book", "ok"), ("unbook", "refused"), ("unbook", "ok")])
        self.assertEqual(log[0]["booking"], bid)
        self.assertEqual(log[0]["remote"], "127.0.0.1")
        self.assertEqual(log[2]["name"], "Alice")

    def test_overlap_warning_names_who(self):
        self.assertEqual(self.book(name="alice", gpus=[2, 3])[0], 200)
        code, r = self.book(name="bob", gpus=[3], from_=wall(tomorrow(3, 30)), to=wall(tomorrow(5)))
        self.assertEqual(code, 200, r)
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("by alice", r["warnings"][0])

    def test_validation(self):
        bad = [dict(gpus=[8]), dict(gpus=[]), dict(gpus=["0"]), dict(gpus=[True]), dict(gpus="0-3"),
               dict(to=wall(tomorrow(2))), dict(to=wall(tomorrow(3) + timedelta(days=7, minutes=30))),
               dict(from_=wall(tomorrow(3) - timedelta(days=3)), to=wall(tomorrow(3) - timedelta(days=2))),
               dict(from_="tomorrow 03:00"), dict(note="x" * 201), dict(note="a\x01b"),
               dict(name=""), dict(name="-rf"), dict(name="a;b"), dict(name="x" * 33), dict(name=None)]
        for kw in bad:
            code, r = self.book(**kw)
            self.assertEqual(code, 400, (kw, r))
            self.assertFalse(r["ok"])
        self.assertEqual(self.bookings(), [])
        self.assertEqual(len(self.weblog()), len(bad))  # refusals are logged too
        self.assertEqual(self.book(to=wall(tomorrow(3) + timedelta(days=7)))[0], 200)  # exactly 7 days is allowed

    def test_unbook_refuses_terminal_bookings_and_bad_ids(self):
        (self.root / "bookings").mkdir(exist_ok=True)
        other = dict(id="carol-20991231-0300-abcd", user="carol", name="carol", gpus=[1], start=4102444800,
                     end=4102448400, note="", created=0)
        (self.root / "bookings" / f"{other['id']}.json").write_text(json.dumps(other))
        code, r = self.req("/unbook", dict(id=other["id"], name="carol"))
        self.assertEqual(code, 403)
        self.assertIn("booked from a terminal by carol", json.loads(r)["message"])
        self.assertEqual(self.req("/unbook", dict(id="../config", name="x"))[0], 400)
        self.assertEqual(self.req("/unbook", dict(id="nope-1", name="x"))[0], 404)
        self.assertEqual(len(self.bookings()), 1)

    def test_methods_and_content_types(self):
        self.assertEqual(self.req("/book")[0], 405)
        self.assertEqual(self.req("/unbook")[0], 405)
        self.assertEqual(self.req("/book", b"name=alice", ctype="application/x-www-form-urlencoded")[0], 415)
        self.assertEqual(self.req("/book", b"[1, 2]")[0], 400)
        self.assertEqual(self.req("/book", b"{" * 5000)[0], 413)
        self.assertEqual(self.req("/book", dict(name="alice"), method="DELETE")[0], 405)
        self.assertEqual(self.req("/nope")[0], 404)
        self.assertEqual(self.bookings(), [])


if __name__ == "__main__":
    unittest.main()
