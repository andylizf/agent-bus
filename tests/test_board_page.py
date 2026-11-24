#!/usr/bin/env python3
"""Checks tools/board_page.py's render on a made-up board: free and busy GPUs, an occupant with no board entry,
a booking that overlaps another, and the how-to commands. `python3 tests/test_board_page.py [--write OUT.html]`
also writes the page, for a look in a browser."""
import json, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import board_page  # noqa: E402

NOW = 1795000000
H = 3600
GIB = 1024


def fixture():
    def gpu(i, procs, util):
        return dict(gpu=i, bus=f"00000000:{0x1A + 0x20 * i:02X}:00.0", total_mb=275040, util=util,
                    mem_mb=sum(m for _, m in procs) + 18, procs=[dict(user=u, mem_mb=m) for u, m in procs])
    gpus = [gpu(0, [("alice", 140 * GIB)], 97), gpu(1, [("alice", 140 * GIB)], 95), gpu(2, [], 0),
            gpu(3, [("bob", 60 * GIB), ("carol", 40 * GIB)], 50), gpu(4, [("dave", 200 * GIB)], 99),
            gpu(5, [], 0), gpu(6, [("erin", 300)], 0), gpu(7, [], 0)]
    entries = [dict(id="alice-train", user="alice", host="gpubox", gpus=[0, 1], until=NOW + 5 * H, state="live",
                    task="alice: pretraining sweep <run 7>", exclusive=True, started=NOW - 2 * H),
               dict(id="bob-eval", user="bob", host="gpubox", gpus=[3], until=NOW + H, state="unattended",
                    task="bob: eval", exclusive=False, started=NOW - H),
               dict(id="frank-hold", user="frank", host="gpubox", gpus=[7], until=NOW + 2 * H, state="live",
                    task="frank: warming up", exclusive=False, started=NOW - 600)]
    bookings = [dict(id="carol-1", user="carol", name="carol", gpus=[0, 1, 2, 3], start=NOW + 3 * H, end=NOW + 6 * H,
                     note="ablation grid", created=NOW - H),
                dict(id="bob-2", user="bob", name="bob", gpus=[5], start=NOW + 26 * H, end=NOW + 30 * H,
                     note="", created=NOW - H)]
    usage = [dict(ts=NOW - k * 120, gpus=[dict(gpu=g["gpu"], util=g["util"], mem_mb=g["mem_mb"],
                                               users=board_page.users_of(g["procs"])) for g in gpus])
             for k in range(0, 300)]
    return dict(host="gpubox", tz="America/New_York", now=NOW, gpus=gpus, bookings=bookings, entries=entries, usage=usage)


class Render(unittest.TestCase):
    def setUp(self):
        self.page = board_page.render(fixture())

    def test_free_count_and_states(self):
        self.assertIn("<b>3</b> of 8 free", self.page)  # GPUs 2, 5, 6 (erin's 300 MiB context does not count)
        self.assertIn('aria-label="GPU 7, claimed"', self.page)  # frank's entry, no process

    def test_unregistered_occupant_marked(self):
        self.assertIn("carol", self.page)
        self.assertIn('class="blk unreg', self.page)  # dave on GPU 4 and carol on GPU 3 have no entry
        self.assertIn("dave, carol", self.page.replace("carol, dave", "dave, carol"))

    def test_overlap_marked(self):
        self.assertIn('class="blk book clash"', self.page)  # carol's booking meets alice's entry on 0-1

    def test_web_bookings_told_apart_by_name(self):
        d = fixture()  # two bookings made on the web page: one unix user (the server's), two names
        d["bookings"] = [dict(id=f"srv-{n}", user="srv", name=n, gpus=[6], start=NOW + 30 * H, end=NOW + 32 * H, note="",
                              created=NOW) for n in ("gina", "hank")]
        page = board_page.render(d, web=dict(owner="srv"))
        self.assertEqual(page.count('class="blk book clash"'), 2)
        self.assertEqual(page.count('class="rm"'), 2)
        self.assertNotIn('class="rm"', self.page)  # the static page has no Remove buttons

    def test_escaped(self):
        self.assertIn("&lt;run 7&gt;", self.page)
        self.assertNotIn("<run 7>", self.page)

    def test_howto(self):
        for c in ("bus.py book --gpus 0-3 --from 22:00 --to 01:00", "bus.py unbook &lt;id&gt;", "bus.py cal"):
            self.assertIn(c, self.page)


if __name__ == "__main__":
    if "--write" in sys.argv:
        Path(sys.argv[sys.argv.index("--write") + 1]).write_text(board_page.render(fixture()))
    else:
        unittest.main()
