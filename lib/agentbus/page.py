"""`bus page`: the read-only board page, rendered by one's own install (design 3.11).

v2 runs with their class, bookings with their state and objections, the old board's entries as a separate read-only
section (parsed as untrusted data by the legacy adapter), and the live nvidia-smi view with processes of users not on
either board shown as off-board use. GET only; nothing is written anywhere. Bounds: 16 handler threads, overload
answered 503; one 15 s deadline per connection, enforced by shutting the socket down; collection in one refresher
thread with 20 s subprocess timeouts; the last good page served with a banner when a refresh fails.
"""
import html, socket, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from . import bookings as bk
from . import core, legacy, runner

THREADS = 16
CONN_DEADLINE_S = 15.0
REFRESH_S = 20.0


def collect(conf, legacy_path=None):
    data = dict(t=core.now(), host=core.HOST, errors=[])
    try:
        with core.Board.from_conf(conf) as b:
            entries = bk.entries_with_occupancy(b)
            data.update(identity="ok" if b.ok else "changed", board=b.path, ngpus=b.ngpus,
                        entries=[core.clean(e) for e in entries],
                        bookings=bk.evaluate(b, entries=entries), warnings=b.warnings)
    except (OSError, core.Refused) as e:
        data["errors"].append(f"v2 board: {e}")
        data.setdefault("entries", [])
        data.setdefault("bookings", [])
    try:
        with legacy.Old(legacy_path or conf.get("legacy_path")) as o:
            data["legacy"] = legacy.ls_json(o)
    except (OSError, core.Refused) as e:
        data["legacy"] = []
        data["errors"].append(f"old board: {e}")
    ok, idx, apps, util = runner.nvidia_view(timeout=20)
    gpus = {i: dict(gpu=i, uuid=u, util=util.get(i), procs=[]) for i, u in idx.items()}
    by_uuid = {u: i for i, u in idx.items()}
    for uuid, pid in apps:
        if uuid in by_uuid:
            uid = core.proc_uid(pid)
            gpus[by_uuid[uuid]]["procs"].append(core.owner_name(uid) if uid is not None else "?")
    if not ok:
        data["errors"].append("nvidia-smi failed")
    data["gpus"] = [gpus[i] for i in sorted(gpus)]
    return data


def esc(x):
    return html.escape(str(x if x is not None else ""))


def when(t):
    return datetime.fromtimestamp(t).strftime("%a %m-%d %H:%M") if isinstance(t, (int, float)) else "-"


def render(d, banner=None):
    on_board = {e["user"] for e in d.get("entries", [])} | {str(a.get("file_owner")) for a in d.get("legacy", [])}
    claimed = {}
    for e in d.get("entries", []):
        for g in e["gpus"]:
            claimed.setdefault(g, []).append(e["user"])
    rows = []
    for g in d.get("gpus", []):
        users = sorted(set(g["procs"]))
        off = [u for u in users if u not in on_board]
        rows.append(f"<tr><td>gpu{g['gpu']}</td><td>{esc(g['util'])}%</td><td>{esc(', '.join(users) or '-')}</td>"
                    f"<td>{esc(', '.join(sorted(set(claimed.get(g['gpu'], [])))) or '-')}</td>"
                    f"<td>{'off-board use: ' + esc(', '.join(off)) if off else ''}</td></tr>")
    ents = "".join(
        f"<tr><td>{esc(e['id'])}</td><td>{esc(e['user'])}</td><td>{esc(core.gpu_text(e['gpus']))}</td>"
        f"<td>{esc(e['class'])}{' (idle)' if e.get('idle') else ''}</td><td>{esc(e['state'])}</td>"
        f"<td>{esc(e['occupancy'] if e['occupancy'] == 'live' else 'occupancy unknown')}</td>"
        f"<td>{esc(when(e['until']))}</td><td>{esc(e['kind'])}</td><td>{esc(e.get('note'))}</td></tr>"
        for e in d.get("entries", []))
    books = "".join(
        f"<tr><td>{esc(b['id'])}</td><td>{esc(b['user'])}</td><td>{esc(core.gpu_text(b['gpus']))}</td>"
        f"<td>{esc(when(b['start']))} - {esc(when(b['end']))}</td><td>{esc(b['state'])}</td>"
        f"<td>{esc(when(b.get('objection_close')))}</td><td>{esc(b['kind'])}</td><td>{esc(b.get('note'))}</td>"
        f"<td>{'<br>'.join(esc(o['user'] + ': ' + o['reason']) for o in b.get('objections', [])) or esc(b.get('why'))}</td></tr>"
        for b in d.get("bookings", []) if b.get("state") != "unpublished")
    old = "".join(
        f"<tr><td>{esc(a.get('id'))}</td><td>{esc(a.get('file_owner'))}</td><td>{esc(a.get('gpus'))}</td>"
        f"<td>{esc(a.get('state'))}</td><td>{esc(when(a.get('until')))}</td><td>{esc(str(a.get('task', ''))[:80])}</td></tr>"
        for a in d.get("legacy", []))
    notes = "".join(f"<p class=warn>{esc(x)}</p>" for x in (([banner] if banner else []) + d.get("errors", [])
                                                            + d.get("warnings", [])))
    ident = "" if d.get("identity", "ok") == "ok" else "<p class=warn>board identity changed: shown as read</p>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GPU board</title><style>
:root{{--bg:#fff;--fg:#1b1b1b;--mute:#666;--line:#ddd;--warn:#9a3b00}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#eee;--mute:#aaa;--line:#333;--warn:#ffb070}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:16px;max-width:1200px}}
table{{border-collapse:collapse;width:100%;margin-bottom:20px}}td,th{{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}}
th{{color:var(--mute);font-weight:600}}.warn{{color:var(--warn)}}.wrap{{overflow-x:auto}}h2{{font-size:16px;margin:20px 0 6px}}
</style></head><body>
<h1 style="font-size:20px">GPUs of {esc(d.get('host'))} <span style="color:var(--mute);font-size:13px">as of {esc(when(d.get('t')))}; read-only</span></h1>
{notes}{ident}
<h2>Now on each GPU</h2><div class=wrap><table><tr><th>GPU</th><th>use</th><th>processes of</th><th>v2 entries of</th><th></th></tr>{''.join(rows)}</table></div>
<h2>v2 runs</h2><div class=wrap><table><tr><th>id</th><th>user</th><th>GPUs</th><th>class</th><th>state</th><th>heartbeat</th><th>until</th><th>kind</th><th>note</th></tr>{ents or '<tr><td colspan=9>none</td></tr>'}</table></div>
<h2>Bookings</h2><div class=wrap><table><tr><th>id</th><th>user</th><th>GPUs</th><th>window</th><th>state</th><th>objections until</th><th>kind</th><th>note</th><th>objections / why</th></tr>{books or '<tr><td colspan=9>none</td></tr>'}</table></div>
<h2>Old board (read-only)</h2><div class=wrap><table><tr><th>id</th><th>file owner</th><th>GPUs</th><th>state</th><th>until</th><th>task</th></tr>{old or '<tr><td colspan=6>none</td></tr>'}</table></div>
</body></html>"""


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
        except Exception as e:  # the last good page stays, with a banner
            with self.lock:
                self.err = f"{type(e).__name__}: {e}"

    def loop(self):
        while True:
            self.refresh()
            time.sleep(REFRESH_S)

    def current(self):
        with self.lock:
            h, err, at = self.html, self.err, self.ok_at
        if h is None:
            return 503, "<p>collecting; try again in a few seconds</p>" + (f"<p>{esc(err)}</p>" if err else "")
        if err:
            banner = (f"<p class=warn>refresh failed ({esc(err)}); showing the page from "
                      f"{esc(when(at))}</p>")
            h = h.replace("<body>", "<body>" + banner, 1)
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
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
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

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if code == 405:
                self.send_header("Allow", "GET")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.split("?", 1)[0] != "/":
                return self._send(404, "not found\n", "text/plain; charset=utf-8")
            self._send(*page.current())

        def _no(self):
            self._send(405, "read-only page: GET only\n", "text/plain; charset=utf-8")

        do_POST = do_PUT = do_DELETE = do_PATCH = _no
    return H


def serve(port=8300, host="127.0.0.1", collect_fn=None, threads=THREADS, deadline=CONN_DEADLINE_S):
    conf = core.load_conf()
    page = Page(collect_fn or (lambda: collect(conf)))
    srv = Server((host, port), make_handler(page), threads, deadline)
    threading.Thread(target=page.loop, daemon=True).start()
    return srv, page
