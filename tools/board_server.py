#!/usr/bin/env python3
"""The board page, served live on the GPU host, with booking from the page.

  board_server.py [--board DIR] [--port 8300]

It listens on 127.0.0.1 only; people reach it through ssh (`ssh -L 8300:localhost:8300 <gpu-host>`) or an editor's
port forwarding. GET / renders board_page.py's page from this host's own data, at most CACHE_S seconds old.
POST /book and POST /unbook take JSON and run bus.py's own `book --as <name>` and `unbook` (an argv list, never a
shell), so a booking made on the page is the same record as one made in a terminal: owned by the server's unix
user, shown under the name typed on the page. Names are an honor system. Each write is appended to
<board>/bookings-web.log. Python 3.9, standard library only.
"""
import argparse, getpass, json, os, re, subprocess, sys, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_page  # noqa: E402

HOST = "127.0.0.1"   # the only interface: anyone who reaches the page can book under any name
PORT = 8300
CACHE_S = 20         # a page at most this old is served again instead of collecting anew
MAX_BODY = 4096
MAX_SPAN = 7 * 86400
NOTE_MAX = 200
NGPUS = 8
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,31}")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
WALL_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def check_name(v):
    v = v.strip() if isinstance(v, str) else ""
    if not NAME_RE.fullmatch(v):
        raise Refused(400, "Name: 1 to 32 letters, digits, spaces, dots, dashes or underscores, starting with a letter or digit.")
    return v


def wall(v, what):
    """'YYYY-MM-DDTHH:MM' in the board's zone (what a datetime-local field sends) -> epoch seconds."""
    if not isinstance(v, str) or not WALL_RE.fullmatch(v):
        raise Refused(400, f"{what}: give a date and time.")
    try:
        return int(datetime.strptime(v, "%Y-%m-%dT%H:%M").replace(tzinfo=board_page.TZ).timestamp())
    except ValueError:
        raise Refused(400, f"{what}: {v} is not a date and time.") from None


def validate_book(body, now):
    name = check_name(body.get("name"))
    gpus = body.get("gpus")
    if (not isinstance(gpus, list) or not gpus
            or not all(type(g) is int and 0 <= g < NGPUS for g in gpus)):
        raise Refused(400, f"GPUs: pick at least one of 0-{NGPUS - 1}.")
    start, end = wall(body.get("from"), "From"), wall(body.get("to"), "To")
    if end <= start:
        raise Refused(400, "The booking ends before it starts.")
    if end - start > MAX_SPAN:
        raise Refused(400, "A booking can be at most 7 days long.")
    if end <= now:
        raise Refused(400, "The booking ends in the past.")
    note = body.get("note", "")
    if not isinstance(note, str):
        raise Refused(400, "Note: text only.")
    note = " ".join(note.split())
    if any(ord(c) < 32 or ord(c) == 127 for c in note):
        raise Refused(400, "Note: no control characters.")
    if len(note) > NOTE_MAX:
        raise Refused(400, f"Note: at most {NOTE_MAX} characters.")
    return dict(name=name, gpus=sorted(set(gpus)), start=body["from"].replace("T", " "), end=body["to"].replace("T", " "),
                note=note)


class Board:
    def __init__(self, board, collect=None):
        self.root = Path(board)
        self.bus = self.root / "bin" / "bus.py"
        self.owner = getpass.getuser()
        self.log_path = self.root / "bookings-web.log"
        self.collect = collect or (lambda: board_page.collect(str(self.root)))
        self.lock = threading.Lock()
        self.cached = (0.0, None)

    def page(self):
        with self.lock:  # one collect at a time; the others wait for its result
            ts, html = self.cached
            if html is None or time.time() - ts > CACHE_S:
                html = board_page.render(self.collect(), web=dict(owner=self.owner))
                self.cached = (time.time(), html)
            return html

    def stale(self):
        with self.lock:
            self.cached = (0.0, None)

    def run_bus(self, *args):
        env = dict(os.environ, AGENT_BUS_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(self.bus), *args], capture_output=True, text=True, env=env, timeout=30)

    def log(self, **kw):
        line = json.dumps(dict(time=datetime.now(board_page.TZ).strftime("%Y-%m-%d %H:%M:%S %Z"), **kw), ensure_ascii=False)
        fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o664)
        with os.fdopen(fd, "a") as f:
            f.write(line + "\n")

    def book(self, body, remote):
        try:
            b = validate_book(body, int(time.time()))
        except Refused as e:
            self.log(action="book", result="refused: " + e.message, name=str(body.get("name"))[:40], booking=None, remote=remote)
            raise
        r = self.run_bus("book", f"--gpus={','.join(map(str, b['gpus']))}", f"--from={b['start']}", f"--to={b['end']}",
                         f"--note={b['note']}", f"--as={b['name']}")
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        m = next((re.match(r"booked (\S+):", l) for l in lines if l.startswith("booked ")), None)
        if r.returncode or not m:
            msg = (r.stderr.strip() or r.stdout.strip() or f"bus.py book exited {r.returncode}").splitlines()[-1]
            self.log(action="book", result="failed: " + msg, name=b["name"], booking=None, remote=remote)
            raise Refused(400, msg)
        self.stale()
        self.log(action="book", result="ok", name=b["name"], booking=m[1], remote=remote, gpus=b["gpus"],
                 start=b["start"], end=b["end"])
        return dict(ok=True, id=m[1], lines=lines, warnings=[l for l in lines if l.startswith("warning:")],
                    message=lines[-1])

    def unbook(self, body, remote):
        bid, typed = body.get("id"), body.get("name")
        try:
            if not isinstance(bid, str) or not ID_RE.fullmatch(bid):
                raise Refused(400, "That is not a booking id.")
            typed = check_name(typed)
            try:
                b = json.loads((self.root / "bookings" / f"{bid}.json").read_text())
            except (OSError, ValueError):
                raise Refused(404, f"No booking {bid}: it was removed or has ended.") from None
            if b.get("user") != self.owner:
                raise Refused(403, f"{bid} was booked from a terminal by {b.get('user')}; only they can remove it "
                                   f"(bus.py unbook {bid}).")
            if typed.casefold() != str(b.get("name") or b.get("user")).strip().casefold():
                raise Refused(403, "That is not the name on this booking.")
        except Refused as e:
            self.log(action="unbook", result="refused: " + e.message, name=str(typed)[:40], booking=str(bid)[:120],
                     remote=remote)
            raise
        r = self.run_bus("unbook", bid)
        if r.returncode:
            msg = (r.stderr.strip() or r.stdout.strip() or f"bus.py unbook exited {r.returncode}").splitlines()[-1]
            self.log(action="unbook", result="failed: " + msg, name=typed, booking=bid, remote=remote)
            raise Refused(400, msg)
        self.stale()
        self.log(action="unbook", result="ok", name=typed, booking=bid, remote=remote)
        return dict(ok=True, id=bid, lines=[r.stdout.strip()], message=r.stdout.strip())


class Handler(BaseHTTPRequestHandler):
    board = None  # set by serve()
    WRITES = {"/book": "book", "/unbook": "unbook"}

    def send(self, status, body, ctype):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if status == 405:
            self.send_header("Allow", "POST")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, obj):
        self.send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in self.WRITES:
            return self.send_json(405, dict(ok=False, message="Use POST."))
        if path != "/":
            return self.send(404, "not found\n", "text/plain; charset=utf-8")
        try:
            html = self.board.page()
        except Exception as e:  # collect failed (nvidia-smi, the board): say so instead of a blank page
            return self.send(500, f"could not build the page: {e}\n", "text/plain; charset=utf-8")
        self.send(200, html, "text/html; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in self.WRITES:
            return self.send(404, "not found\n", "text/plain; charset=utf-8")
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self.send_json(415, dict(ok=False, message="Send JSON."))
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= MAX_BODY:
            return self.send_json(413 if n > MAX_BODY else 400, dict(ok=False, message="Bad request size."))
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self.send_json(400, dict(ok=False, message="Send a JSON object."))
        try:
            out = getattr(self.board, self.WRITES[path])(body, self.client_address[0])
        except Refused as e:
            return self.send_json(e.status, dict(ok=False, message=e.message))
        self.send_json(200, out)

    def do_PUT(self):
        self.send_json(405, dict(ok=False, message="Use POST."))

    do_DELETE = do_PATCH = do_PUT


def serve(board, port=PORT, collect=None):
    handler = type("BoardHandler", (Handler,), dict(board=Board(board, collect)))
    httpd = ThreadingHTTPServer((HOST, port), handler)
    httpd.daemon_threads = True
    return httpd


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", default=board_page.BOARD, help="the board directory")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    httpd = serve(a.board, a.port)
    print(f"serving {a.board} on http://{HOST}:{a.port}/", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
