"""Collect and serve the public view of the read-only board."""

import json
import os
import pwd
import re
import socket
import subprocess
import threading
import time
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

from . import bookings as bk
from . import core
from .board_view import HISTORY, PAST, ROBOTS, clock, render

THREADS = 16
CONN_DEADLINE_S = 15.0
REFRESH_S = 20.0
NETID = re.compile(r"[a-z][a-z0-9]{1,15}\Z")
BOOKING_STATES = {"standing", "pending", "conflicting", "objected", "over-budget", "lapsed", "invalid"}

# Free text can contain anything. Withhold a note that resembles a path, network address, credential or command.
PRIVATE_TEXT = re.compile(
    r"[/\\=@\x60|$<>;&]|\b(?:[a-z][a-z0-9_-]*\.)+(?:[a-z]{2,}|local)\b|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:[\da-f]{0,4}:){2,}[\da-f]{0,4}|"
    r"\b(?:token|secret|credentials?|bearer|password|passwd|passphrase|api[-_ ]?key|private[-_ ]?key)\b|"
    r"\b(?:cmd|command)\s*:|\b(?:srun|sbatch|curl|wget|scp|ssh|bash|sh|zsh|python[23]?|torchrun|uv run|git|make)\s+\S|"
    r"(?<!\w)--[a-z]|\b[a-z0-9_+.-]{24,}\b",
    re.IGNORECASE,
)


def public_note(note):
    note = " ".join((note or "").split())
    return "[redacted]" if PRIVATE_TEXT.search(note) else note


@lru_cache(maxsize=256)
def lab_user(user):
    """A netID for any account on the node. Who owns a GPU process is visible to everyone logged in there, so
    group membership does not decide it; only a name that is not an account or not netID-shaped is withheld."""
    if user and NETID.fullmatch(user):
        try:
            pwd.getpwnam(user)
            return user
        except KeyError:
            pass
    return "Unknown user"


def smi(query):
    result = subprocess.run([core.NVSMI(), query, "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=8, check=True)
    return [[cell.strip() for cell in line.split(",")] for line in result.stdout.splitlines()
            if line.strip() and not line.startswith("No running")]


def gpu_view():
    models = set()
    by_uuid = {}
    for idx, uuid, used, total, util, name in smi("--query-gpu=index,uuid,memory.used,memory.total,utilization.gpu,name"):
        models.add(name.removeprefix("NVIDIA ").split()[0])
        by_uuid[uuid] = dict(gpu=int(idx), mem_mb=int(used), total_mb=int(total),
                             util=int(util) if util.isdigit() else None, users={}, available=True)
    for uuid, pid, mem in smi("--query-compute-apps=gpu_uuid,pid,used_gpu_memory"):
        gpu = by_uuid.get(uuid)
        if gpu is not None:
            uid = core.proc_uid(int(pid))
            user = lab_user(core.owner_name(uid) if uid is not None else None)
            users = gpu["users"]
            users[user] = users.get(user, 0) + (int(mem) if mem.isdigit() else 0)
    model = models.pop() if len(models) == 1 else "GPU"
    return model, sorted(by_uuid.values(), key=lambda g: g["gpu"])


def usage_history(now):
    """Read the private per-run telemetry tails and reduce them to host-wide GPU utilization by timestamp."""
    per_gpu = {}
    try:
        files = Path(core.STATE(), "usage").glob("*.jsonl")
        for path in files:
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_mtime < now - HISTORY:
                    continue
                with path.open("rb") as f:
                    size = f.seek(0, os.SEEK_END)
                    if size > 256 * 1024:
                        f.seek(size - 256 * 1024)
                        f.readline()
                    else:
                        f.seek(0)
                    for line in f:
                        try:
                            row = json.loads(line)
                            t = row["t"]
                            if type(t) is int and now - HISTORY <= t <= now:
                                for gpu, util in row["util"].items():
                                    if type(util) is int and 0 <= util <= 100:
                                        samples = per_gpu.setdefault(int(gpu), {})
                                        samples[t] = max(util, samples.get(t, 0))
                        except (AttributeError, KeyError, TypeError, ValueError):
                            continue
            except OSError:
                continue
    except OSError:
        pass
    return {gpu: sorted(samples.items()) for gpu, samples in sorted(per_gpu.items())}


def public_entries(entries, bookings):
    by_id = {b["id"]: b for b in bookings}
    output = []
    for e in entries:
        b = by_id.get(e.get("booking"))
        protected = (e["class"] == "scheduled" and b is not None and b["user"] == e["user"]
                     and b["active"] and set(e["gpus"]) <= set(b["gpus"]))
        output.append(dict(user=lab_user(e["user"]), gpus=e["gpus"], started=e["started"], until=e["until"],
                           state=e["state"], occupancy=e["occupancy"], idle=bool(e.get("idle")), protected=protected,
                           note=public_note(e.get("note")), **{"class": e["class"]}))
    return output


def public_bookings(bookings, now):
    return [
        dict(user=lab_user(b["user"]), gpus=b["gpus"], start=b["start"], end=b["end"], state=b["state"],
             objection_close=b.get("objection_close"), note=public_note(b.get("note")),
             objections=[dict(user=lab_user(o["user"]), reason=public_note(o["reason"])) for o in b["objections"]])
        for b in bookings if b["state"] in BOOKING_STATES and now - PAST < b["end"] and b["start"] < now + 7 * 86400
    ]


def collect(conf, *, history=True):
    now = core.now()
    data = dict(t=now, host=core.HOST, board="", model="GPUs", entries=[], bookings=[], gpus=[], notices=[])
    ngpus = 0
    try:
        with core.Board.from_conf(conf) as b:
            if not b.ok:
                raise core.Refused("board identity changed")
            ngpus = b.ngpus
            entries = bk.entries_with_occupancy(b)
            seen = (core.read_private(os.path.join(core.STATE(), "slots.json")) or {}).get("slots", {})
            bookings = bk.evaluate(b, now, seen=seen, entries=entries, record=False)
            data.update(board=b.path, entries=public_entries(entries, bookings),
                        bookings=public_bookings(bookings, now))
            if b.warnings:
                data["notices"].append("Some board records could not be read.")
    except (OSError, core.Refused):
        data["notices"].append("Board records unavailable.")
    try:
        data["model"], data["gpus"] = gpu_view()
        if not data["gpus"]:
            raise ValueError("empty GPU listing")
    except (OSError, ValueError, subprocess.SubprocessError):
        data["notices"].append("Live GPU readings unavailable.")
        data["gpus"] = [dict(gpu=g, available=False, users={}, util=None, mem_mb=0, total_mb=0) for g in range(ngpus)]
    if history:
        data["usage"] = usage_history(now)
    return data


def public_feed(data):
    """Project a board reading onto the fields displayed by the public page."""
    def netid(user):
        return user if NETID.fullmatch(user) else "Unknown user"

    gpus = []
    for gpu in data["gpus"]:
        users = {}
        for user, mem in gpu["users"].items():
            name = netid(user)
            users[name] = users.get(name, 0) + mem
        gpus.append(dict(gpu=gpu["gpu"], mem_mb=gpu["mem_mb"], total_mb=gpu["total_mb"],
                         util=gpu["util"], users=users, available=gpu["available"]))
    entries = [dict(user=netid(entry["user"]), gpus=entry["gpus"], started=entry["started"], until=entry["until"],
                    kind="scheduled" if entry["protected"] else "backfill") for entry in data["entries"]]
    bookings = [dict(user=netid(booking["user"]), gpus=booking["gpus"], start=booking["start"], end=booking["end"],
                     state=booking["state"], objection_close=booking["objection_close"])
                for booking in data["bookings"] if booking["state"] in BOOKING_STATES]
    return dict(t=data["t"], board=data["board"], gpus=gpus, entries=entries, bookings=bookings)


class Page:
    def __init__(self, collect_fn):
        self.collect_fn = collect_fn
        self.html, self.ok_at, self.err = None, 0.0, None
        self.lock = threading.Lock()

    def refresh(self):
        try:
            h = render(self.collect_fn())
            with self.lock:
                self.html, self.ok_at, self.err = h, time.time(), None
        except Exception:  # preserve the last good page without exposing diagnostics
            with self.lock:
                self.err = True

    def loop(self):
        while True:
            self.refresh()
            time.sleep(REFRESH_S)

    def current(self):
        with self.lock:
            h, err, at = self.html, self.err, self.ok_at
        if h is None:
            return 503, "<p>Board data unavailable. Try again in a few seconds.</p>"
        if err:
            banner = f"<p class=warn>Board refresh failed. Showing data from {clock(at)} ET.</p>"
            h = h.replace('<div class="notes" role="status">', '<div class="notes" role="status">' + banner, 1)
        return 200, h


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, threads=THREADS, deadline=CONN_DEADLINE_S):
        super().__init__(addr, handler)
        self.slots = threading.BoundedSemaphore(threads)
        self.deadline = deadline

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(0.1)
                request.sendall((f"HTTP/1.0 503 Service Unavailable\r\nX-Robots-Tag: {ROBOTS}\r\n"
                                 "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
            except OSError:
                pass
            self.shutdown_request(request)
            return
        timer = threading.Timer(self.deadline, self._cut, (request,))
        timer.daemon = True
        timer.start()

        def work():
            try:
                self.finish_request(request, client_address)
            except Exception:
                pass
            finally:
                timer.cancel()
                self.shutdown_request(request)
                self.slots.release()
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _cut(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def make_handler(page):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("X-Robots-Tag", ROBOTS)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def parse_request(self):
            if not super().parse_request():
                return False
            if self.command != "GET":
                self._send(405, "Read-only page.\n", "text/plain; charset=utf-8")
                return False
            return True

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if code == 405:
                self.send_header("Allow", "GET")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_GET(self):
            target = self.requestline.split()[1]
            if target.split("?", 1)[0] != "/":
                return self._send(404, "not found\n", "text/plain; charset=utf-8")
            self._send(*page.current())

    return H


def serve(port=8300, host="127.0.0.1", collect_fn=None, threads=THREADS, deadline=CONN_DEADLINE_S):
    conf = core.load_conf()
    page = Page(collect_fn or (lambda: collect(conf)))
    srv = Server((host, port), make_handler(page), threads, deadline)
    threading.Thread(target=page.loop, daemon=True).start()
    return srv, page
