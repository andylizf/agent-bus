#!/usr/bin/env python3
"""One self-contained HTML page of the board's GPU host: who is on each GPU now, and the bookings ahead.

  board_page.py collect [--board DIR]            on the GPU host: print the page's input as JSON
  board_page.py render IN.json -o OUT.html       turn that JSON into the page
  board_page.py --ssh HOST -o OUT.html           both, from anywhere: one ssh call runs `collect` on HOST

board_server.py serves the same page live on the GPU host, with a booking form (render's `web` mode).

`collect` reads `bus.py cal --json --days 3`, `bus.py ls --json`, nvidia-smi, the owner (`ps -o user=`) of each GPU
process, and the last 24 h of usage.jsonl. Process ids stay on the host: the JSON carries only users and memory, the
same facts `bus.py log --usage` already shows the group. Python 3.9, standard library only.
"""
import argparse, html, json, os, shlex, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BOARD = str(Path(__file__).resolve().parent.parent)  # the board is the directory above tools/, as for bin/
TZ = ZoneInfo("America/New_York")
SMALL_MB = 1024      # a process below this (a stray CUDA context) does not make a GPU busy
HISTORY = 24 * 3600  # seconds of usage.jsonl the page's utilization strips cover
PAST, AHEAD = 3 * 3600, 72 * 3600  # the calendar's window around now
SLOTS = 8            # categorical colour slots for users


# ---------------------------------------------------------------- collect (runs on the GPU host)

def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def usage_tail(path, since):
    """usage.jsonl lines with ts >= since, read from the end of the file."""
    try:
        f = open(path, "rb")
    except OSError:
        return []
    with f:
        f.seek(0, 2)
        size, chunk = f.tell(), 1 << 22
        while True:
            f.seek(max(0, size - chunk))
            lines = f.read().decode("utf-8", "replace").splitlines()
            if chunk < size:
                lines = lines[1:]  # the first line is cut
            first = next((json.loads(l) for l in lines if l.strip()), None)
            if chunk >= size or first is None or first.get("ts", 0) < since:
                break
            chunk *= 2
    out = []
    for l in lines:
        try:
            s = json.loads(l)
        except ValueError:
            continue
        if s.get("ts", 0) >= since:
            out.append(dict(ts=s["ts"], gpus=[dict(gpu=g["gpu"], util=g["util"], mem_mb=g["mem_mb"],
                                                   users=users_of(g.get("procs", []))) for g in s["gpus"]]))
    return out


def users_of(procs):
    """{user: MiB} summed over a GPU's processes."""
    out = {}
    for p in procs:
        mem = p.get("mem_mb") if isinstance(p.get("mem_mb"), int) else 0
        out[p["user"]] = out.get(p["user"], 0) + mem
    return out


def collect(board):
    bus = [sys.executable, str(Path(board) / "bin" / "bus.py")]
    cal = json.loads(run(*bus, "cal", "--days", "3", "--json"))
    ls = json.loads(run(*bus, "ls", "--json"))
    q = "--format=csv,noheader,nounits"
    gpus, by_bus = [], {}
    for line in run("nvidia-smi", "--query-gpu=index,pci.bus_id,memory.used,memory.total,utilization.gpu,name", q).splitlines():
        i, bid, used, total, util, name = [x.strip() for x in line.split(",", 5)]
        g = dict(gpu=int(i), bus=bid, mem_mb=int(used), total_mb=int(total), util=int(util) if util.isdigit() else None,
                 name=name, procs=[])
        gpus.append(g)
        by_bus[bid] = g
    owner = {}
    for line in run("nvidia-smi", "--query-compute-apps=gpu_bus_id,pid,used_memory", q).splitlines():
        bid, pid, mem = [x.strip() for x in line.split(",")]
        if pid not in owner:
            owner[pid] = run("ps", "-o", "user=", "-p", pid).strip() or "?"
        if bid in by_bus:
            by_bus[bid]["procs"].append(dict(user=owner[pid], mem_mb=int(mem) if mem.isdigit() else 0))
    started = {a["id"]: a.get("started") for a in ls}
    for e in cal["entries"]:
        e["started"] = started.get(e["id"])
    host = run("hostname", "-s").strip()
    return dict(host=host, board=str(board), tz=cal["tz"], now=int(time.time()), gpus=gpus, bookings=cal["bookings"],
                entries=cal["entries"], usage=usage_tail(Path(board) / "usage.jsonl", int(time.time()) - HISTORY))


# ---------------------------------------------------------------- render

def esc(s):
    return html.escape(str(s), quote=True)


def gib(mb):
    return mb / 1024


def gpu_list(gs):
    gs, runs = sorted(gs), []
    for g in gs:
        if runs and g == runs[-1][1] + 1:
            runs[-1][1] = g
        else:
            runs.append([g, g])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def clock(t, day=False):
    d = datetime.fromtimestamp(t, TZ)
    return d.strftime("%a %H:%M") if day else d.strftime("%H:%M")


def span(a, b, now):
    """'21:00 - 01:00' with the weekday on each end that is not today."""
    today = datetime.fromtimestamp(now, TZ).date()
    f = lambda t: clock(t, datetime.fromtimestamp(t, TZ).date() != today)
    return f"{f(a)}–{f(b)}" if f(a).count(" ") + f(b).count(" ") == 0 else f"{f(a)} – {f(b)}"


def colours(users):
    """user -> slot 1..8, from a hash of the name so a user keeps a colour as others come and go; collisions move on."""
    taken, out = set(), {}
    for u in sorted(users):
        h = sum((i + 1) * ord(c) for i, c in enumerate(u)) % SLOTS
        while h in taken and len(taken) < SLOTS:
            h = (h + 1) % SLOTS
        taken.add(h)
        out[u] = h + 1
    return out


def who(b):
    """A booking's person: the name it was booked under. Bookings made on the web page all belong to the server's unix
    user, so colours and overlaps go by this name."""
    return b.get("name") or b["user"]


def occupants(g):
    """[(user, MiB)] on a GPU, biggest first."""
    return sorted(users_of(g["procs"]).items(), key=lambda kv: -kv[1])


def run_starts(usage, now):
    """(gpu, user) -> when the user's current unbroken stretch on that GPU began, from the snapshots."""
    out = {}
    for s in sorted(usage, key=lambda s: -s["ts"]):  # newest first
        for g in s["gpus"]:
            for u, mb in g["users"].items():
                if mb >= SMALL_MB and out.get((g["gpu"], u), (None, True))[1]:
                    out[(g["gpu"], u)] = (s["ts"], True)
            for k in [k for k in out if k[0] == g["gpu"] and out[k][1] and g["users"].get(k[1], 0) < SMALL_MB]:
                out[k] = (out[k][0], False)  # gap: the stretch began after this snapshot
    return {k: v[0] for k, v in out.items()}


def history_span(usage, now):
    """Seconds the sparklines cover: back to the oldest snapshot within HISTORY, at least an hour."""
    oldest = min((s["ts"] for s in usage if s["ts"] >= now - HISTORY), default=now)
    return min(HISTORY, max(3600, now - oldest))


def spark(usage, gpu, now, span_s):
    """An SVG area of utilization over the last span_s seconds, in 48 bins."""
    W, H, n = 100, 24, 48
    bins = [[] for _ in range(n)]
    for s in usage:
        k = int((s["ts"] - (now - span_s)) * n // span_s)
        if 0 <= k < n:
            for g in s["gpus"]:
                if g["gpu"] == gpu:
                    bins[k].append(g["util"])
    pts = [(i * W / (n - 1), H - 1 - (sum(b) / len(b)) * (H - 2) / 100) for i, b in enumerate(bins) if b]
    if len(pts) < 2:
        return '<span class="spark spark-empty">no history</span>'
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{pts[0][0]:.1f},{H} {line} {pts[-1][0]:.1f},{H}"
    mean = sum(sum(b) for b in bins) / max(1, sum(len(b) for b in bins))
    hrs = f"{span_s / 3600:.0f} h"
    return (f'<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" '
            f'aria-label="GPU {gpu} utilization, last {hrs}, mean {mean:.0f}%"><title>utilization, last {hrs}: mean {mean:.0f}%</title>'
            f'<line x1="0" y1="{H - 0.5}" x2="{W}" y2="{H - 0.5}" class="spark-base"/>'
            f'<polygon points="{area}" class="spark-area"/><polyline points="{line}" class="spark-line"/></svg>')


def tile(g, entries, slot, usage, now, span_s):
    occ = occupants(g)
    big = [(u, mb) for u, mb in occ if mb >= SMALL_MB]
    small = [(u, mb) for u, mb in occ if mb < SMALL_MB]
    mine = [e for e in entries if g["gpu"] in e["gpus"]]
    board_users = {e["user"] for e in mine}
    free = not big and not mine
    state = "free" if free else ("claimed" if not big else "busy")
    total = g["total_mb"]
    bar = "".join(f'<span class="seg" style="width:{100 * mb / total:.2f}%;--c:var(--u{slot[u]})" '
                  f'title="{esc(u)}: {gib(mb):.1f} GiB"></span>' for u, mb in occ)
    who = []
    for u, mb in big:
        flag = "" if u in board_users else '<span class="flag" title="This user has memory on the GPU but no board entry for it">not on board</span>'
        who.append(f'<li class="who"><span class="dot" style="--c:var(--u{slot[u]})"></span>'
                   f'<span class="name">{esc(u)}</span><span class="mem">{gib(mb):.0f} GiB</span>{flag}</li>')
    for u in sorted(board_users - {u for u, _ in big}):
        who.append(f'<li class="who"><span class="dot" style="--c:var(--u{slot[u]})"></span>'
                   f'<span class="name">{esc(u)}</span><span class="mem muted">board entry, no process of theirs</span></li>')
    if free:
        who.append('<li class="free-label"><span class="free-mark" aria-hidden="true"></span>Free</li>')
    tasks = []
    for e in mine:
        tags = ['<span class="tag excl">exclusive</span>' if e.get("exclusive") else '<span class="tag">shared</span>']
        if e.get("state") != "live":
            tags.append(f'<span class="tag warn">{esc(e.get("state"))}</span>')
        until = f'until {clock(e["until"], e["until"] - now > 12 * 3600)}' if e.get("until") else "no end given"
        tasks.append(f'<div class="task"><div class="task-meta"><span class="dot" style="--c:var(--u{slot[e["user"]]})"></span>'
                     f'<b>{esc(e["user"])}</b> {"".join(tags)}<span class="until">{until}</span></div>'
                     f'<p class="task-text">{esc(e.get("task") or "(no task text)")}</p></div>')
    if not tasks and not free:
        tasks.append('<div class="task none">Nothing registered on the board for this GPU.</div>')
    smalls = ""
    if small:
        smalls = '<p class="small">+ ' + ", ".join(f'{esc(u)} {gib(mb):.1f} GiB' for u, mb in small) + "</p>"
    util = "–" if g["util"] is None else f'{g["util"]}<small>%</small>'
    return f'''<article class="tile {state}" aria-label="GPU {g["gpu"]}, {state}">
  <header class="tile-head"><h3>GPU {g["gpu"]}</h3><span class="busid">{esc(g["bus"][-7:-2])}</span>
    <span class="state-pill {state}">{ {"free": "free", "claimed": "claimed, idle", "busy": "in use"}[state] }</span></header>
  <ul class="whos">{"".join(who)}</ul>
  <div class="meter" role="img" aria-label="memory {gib(g["mem_mb"]):.0f} of {gib(total):.0f} GiB">{bar}</div>
  <div class="meter-legend"><span><b>{gib(g["mem_mb"]):.0f}</b> / {gib(total):.0f} GiB</span>{spark(usage, g["gpu"], now, span_s)}
    <span class="util"><span class="util-num">{util}</span> util</span></div>
  {smalls}
  {"".join(tasks)}
</article>'''


def lanes(items):
    """Give each item a lane so overlapping items on one GPU sit one above another."""
    ends = []
    for it in sorted(items, key=lambda it: (it["a"], it["b"])):
        for k, e in enumerate(ends):
            if e <= it["a"]:
                it["lane"], ends[k] = k, it["b"]
                break
        else:
            it["lane"] = len(ends)
            ends.append(it["b"])
    return max(1, len(ends))


def calendar(data, slot, now):
    t0 = datetime.fromtimestamp(now - PAST, TZ).replace(minute=0, second=0, microsecond=0).timestamp()
    t1 = t0 + PAST + AHEAD + 3600
    pct = lambda t: 100 * (min(max(t, t0), t1) - t0) / (t1 - t0)
    starts = run_starts(data["usage"], now)
    rows = {g["gpu"]: [] for g in data["gpus"]}
    for b in data["bookings"]:
        for g in b["gpus"]:
            if g in rows and b["end"] > t0 and b["start"] < t1:
                rows[g].append(dict(kind="book", a=b["start"], b=b["end"], user=who(b), b_=b,
                                    label=who(b), note=b.get("note", "")))
    for e in data["entries"]:
        end = max(e.get("until") or now + 3600, now + 600)
        for g in e["gpus"]:
            if g in rows:
                rows[g].append(dict(kind="live", a=e.get("started") or t0, b=end, user=e["user"],
                                    label=e["user"], note=e.get("task", ""), e=e))
    for g in data["gpus"]:
        board = {e["user"] for e in data["entries"] if g["gpu"] in e["gpus"]}
        for u, mb in occupants(g):
            if mb >= SMALL_MB and u not in board:
                rows[g["gpu"]].append(dict(kind="unreg", a=starts.get((g["gpu"], u), now), b=now + 8 * 3600,
                                           user=u, label=u, note="not on board, end unknown", mb=mb))
    # overlaps: a booking that meets another item on the same GPU
    for items in rows.values():
        for it in items:
            it["clash"] = [o for o in items if o is not it and o["a"] < it["b"] and it["a"] < o["b"]
                           and it["user"] != o["user"] and "book" in (it["kind"], o["kind"])]
    # axis
    ticks, days, lines, nights = [], [], [], []
    t = t0
    while t <= t1:
        d = datetime.fromtimestamp(t, TZ)
        if d.hour == 0:
            lines.append(f'<div class="day-line" style="left:{pct(t):.3f}%"></div>')
            days.append(f'<div class="day-label" style="left:{pct(t):.3f}%">{d.strftime("%a %b %-d")}</div>')
        elif d.hour % 3 == 0 and t - t0 >= 2 * 3600:  # no tick label under the GPU column
            ticks.append(f'<div class="tick{" major" if d.hour % 6 == 0 else ""}" style="left:{pct(t):.3f}%">'
                         f'<span>{d.strftime("%H")}</span></div>')
        if d.hour == 22:
            nights.append(f'<div class="night" style="left:{pct(t):.3f}%;width:{pct(t + 9 * 3600) - pct(t):.3f}%"></div>')
        t += 3600
    first = datetime.fromtimestamp(t0, TZ)
    if first.hour >= 3:
        days.insert(0, f'<div class="day-label first" style="left:0">{first.strftime("%a %b %-d")}</div>')
    if first.hour < 7:  # the window opens inside a night that began yesterday at 22:00
        nights.append(f'<div class="night" style="left:0;width:{pct(t0 + (7 - first.hour) * 3600):.3f}%"></div>')
    nowx = pct(now)
    out = []
    for gi in sorted(rows):
        items = rows[gi]
        n = lanes(items)
        blocks = []
        for it in items:
            a, b = pct(it["a"]), pct(it["b"])
            colour = f'--c:var(--u{slot[it["user"]]})'
            if it["kind"] == "book":
                tip = (f'Booking {it["b_"]["id"]}\n{it["label"]} ({it["b_"]["user"]}), GPUs {gpu_list(it["b_"]["gpus"])}\n'
                       f'{span(it["a"], it["b"], now)}' + (f'\n{it["note"]}' if it["note"] else ""))
                text = f'<b>{esc(it["label"])}</b> <span class="t">{esc(span(it["a"], it["b"], now))}</span> {esc(it["note"])}'
            elif it["kind"] == "live":
                e = it["e"]
                tip = (f'On the board now: {e["user"]}, GPUs {gpu_list(e["gpus"])}\n{e.get("task", "")}\n'
                       f'expected end {clock(e["until"], True) if e.get("until") else "not given"}')
                text = f'<b>{esc(it["user"])}</b> <span class="t">until {esc(span(it["b"], it["b"], now).split("–")[0].strip())}</span> {esc(it["note"])}'
            else:
                tip = f'{it["user"]}: {gib(it["mb"]):.0f} GiB on GPU {gi}, no board entry, end unknown'
                text = f'<b>{esc(it["user"])}</b> <span class="t">not on board</span>'
            if it["clash"]:
                tip += "\noverlaps: " + ", ".join(sorted({o["user"] for o in it["clash"]}))
            blocks.append(f'<div class="blk {it["kind"]}{" clash" if it["clash"] else ""}" '
                          f'style="left:{a:.3f}%;width:{max(b - a, 0.4):.3f}%;top:calc({it["lane"]} * var(--lane));{colour}" '
                          f'title="{esc(tip)}"><span class="blk-text">{text}</span></div>')
        out.append(f'<div class="cal-row" data-gpu="{gi}" style="--lanes:{n}"><div class="cal-gpu">GPU {gi}</div>'
                   f'<div class="cal-track">{"".join(blocks)}</div></div>')
    return f'''<div class="cal-scroll" tabindex="0" aria-label="Calendar, scrolls sideways">
  <div class="cal" data-t0="{t0:.0f}" data-t1="{t1:.0f}" style="min-width:calc(var(--gpucol) + {(t1 - t0) / 3600:.0f} * var(--hour))">
    <div class="cal-axis"><div class="cal-gpu"></div><div class="cal-track">{"".join(days)}{"".join(lines)}{"".join(ticks)}</div></div>
    <div class="cal-body">
      <div class="cal-under"><div class="cal-gpu"></div><div class="cal-track">{"".join(nights)}
        <div class="past" style="width:{nowx:.3f}%"></div>
        {"".join(lines)}</div></div>
      {"".join(out)}
      <div class="cal-over"><div class="cal-gpu"></div><div class="cal-track">
        <div class="now-line" style="left:{nowx:.3f}%"><span>now {clock(now)}</span></div></div></div>
    </div>
  </div>
</div>'''


def remove_cell(b, owner):
    """web mode: a Remove button for bookings the server's own user made (bus.py unbook removes only those)."""
    if b["user"] == owner:
        return (f'<td><button class="rm" type="button" data-id="{esc(b["id"])}" data-name="{esc(b.get("name") or b["user"])}">'
                f'Remove</button></td>')
    return (f'<td><span class="muted" title="Booked from a terminal by {esc(b["user"])}; they remove it with '
            f'bus.py unbook {esc(b["id"])}">terminal only</span></td>')


def booking_table(data, slot, now, web=None):
    bs = sorted(data["bookings"], key=lambda b: b["start"])
    if not bs:
        if web:
            return '<p class="empty">No bookings in the next 3 days. Book GPUs with the form above.</p>'
        return ('<p class="empty">No bookings in the next 3 days. Book GPUs with '
                '<code>bus.py book</code> (below); the booking shows up here the next time the page is built.</p>')
    alias = lambda b: "" if (b.get("name") or b["user"]) == b["user"] else f' <span class="muted">({esc(b["user"])})</span>'
    rows = "".join(f'<tr><td><span class="dot" style="--c:var(--u{slot[who(b)]})"></span>{esc(who(b))}'
                   f'{alias(b)}</td>'
                   f'<td class="num">{gpu_list(b["gpus"])}</td><td class="num">{esc(span(b["start"], b["end"], now))}</td>'
                   f'<td>{esc(b.get("note", ""))}</td><td><code>{esc(b["id"])}</code></td>'
                   f'{remove_cell(b, web["owner"]) if web else ""}</tr>' for b in bs)
    return (f'<div class="table-scroll"><table><thead><tr><th>Who</th><th>GPUs</th><th>When (ET)</th><th>Note</th>'
            f'<th>Booking id</th>{"<th></th>" if web else ""}</tr></thead><tbody>{rows}</tbody></table></div>')


CSS = r"""
/* Layout: a header strip, an 8-slot "board" of GPU tiles (4 x 2 like the baseboard), then a GPU x time calendar. */
:root {
  --bg: #eef1f4; --surface: #ffffff; --surface-2: #f6f8fa; --ink: #0e1621; --ink-2: #485463; --muted: #7a8594;
  --line: #dde2e8; --line-2: #c7ced7; --accent: #0d6b69; --now: #0e1621; --night: rgba(30, 50, 90, 0.05);
  --past: rgba(120, 130, 145, 0.08);
  --free: #0a8f3c; --free-bg: #e4f5ea; --warn: #b4501f; --warn-bg: #fdeee6; --crit: #c23434;
  --u1: #2a78d6; --u2: #eb6834; --u3: #1baf7a; --u4: #eda100; --u5: #e87ba4; --u6: #008300; --u7: #6250d6; --u8: #e34948;
  --display: "Archivo", "Arial Narrow", system-ui, sans-serif; --body: "Archivo", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
  --lane: 30px; --gpucol: 62px; --hour: 36px;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0c1015; --surface: #151b22; --surface-2: #1b222b; --ink: #e9edf2; --ink-2: #aab4c0; --muted: #7d8896;
  --line: #262f3a; --line-2: #36414e; --accent: #4fc1bd; --now: #f2f4f7; --night: rgba(120, 150, 210, 0.06);
  --past: rgba(0, 0, 0, 0.22);
  --free: #3ccf72; --free-bg: #12301e; --warn: #f0935f; --warn-bg: #3a2214; --crit: #ef6b6b;
  --u1: #3987e5; --u2: #d95926; --u3: #199e70; --u4: #c98500; --u5: #d55181; --u6: #2a9a2a; --u7: #9085e9; --u8: #e66767;
  color-scheme: dark; } }
:root[data-theme="dark"] {
  --bg: #0c1015; --surface: #151b22; --surface-2: #1b222b; --ink: #e9edf2; --ink-2: #aab4c0; --muted: #7d8896;
  --line: #262f3a; --line-2: #36414e; --accent: #4fc1bd; --now: #f2f4f7; --night: rgba(120, 150, 210, 0.06);
  --past: rgba(0, 0, 0, 0.22);
  --free: #3ccf72; --free-bg: #12301e; --warn: #f0935f; --warn-bg: #3a2214; --crit: #ef6b6b;
  --u1: #3987e5; --u2: #d95926; --u3: #199e70; --u4: #c98500; --u5: #d55181; --u6: #2a9a2a; --u7: #9085e9; --u8: #e66767;
  color-scheme: dark; }
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { background: var(--bg); color: var(--ink); font: 14px/1.45 var(--body); margin: 0; }
.wrap > * { min-width: 0; }
.wrap { max-width: 1320px; margin: 0 auto; padding-inline: 16px; padding-block: 20px 40px; display: grid; gap: 28px; }
code, .mono { font-family: var(--mono); font-size: 0.86em; }
.muted { color: var(--muted); }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; background: var(--c); flex: none; }

/* header */
.top { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 12px 24px; }
.top h1 { font: 800 clamp(30px, 5vw, 44px)/1 var(--display); font-stretch: 78%; letter-spacing: -0.01em; margin: 0; text-wrap: balance; }
.top h1 span { color: var(--accent); }
.sub { color: var(--ink-2); margin: 6px 0 0; }
.summary { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 11px; border-radius: 999px; background: var(--surface);
  border: 1px solid var(--line); font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
.chip.free b { color: var(--free); }
.stamp { font-family: var(--mono); font-size: 12px; color: var(--ink-2); }
.stamp.stale { color: var(--warn); font-weight: 700; }

h2 { font: 700 13px/1 var(--body); text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-2); margin: 0 0 12px;
  display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
h2 small { text-transform: none; letter-spacing: 0; font-weight: 400; color: var(--muted); font-size: 12.5px; }

/* GPU tiles */
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 1100px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .tiles { grid-template-columns: minmax(0, 1fr); } }
.tile { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 14px 12px;
  display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.tile.free { background: var(--free-bg); border-color: color-mix(in srgb, var(--free) 35%, var(--line)); }
.tile-head { display: flex; align-items: baseline; gap: 8px; }
.tile-head h3 { font: 800 20px/1 var(--display); font-stretch: 80%; margin: 0; }
.busid { font: 11px var(--mono); color: var(--muted); }
.state-pill { margin-left: auto; font-size: 11.5px; font-weight: 700; padding: 2px 9px; border-radius: 999px;
  background: var(--surface-2); color: var(--ink-2); border: 1px solid var(--line); }
.state-pill.free { background: var(--free); color: var(--surface); border-color: var(--free); }
.state-pill.busy { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.whos { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; min-height: 26px; }
.who { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.who .name { font-weight: 700; font-size: 16px; }
.who .mem { color: var(--ink-2); font-variant-numeric: tabular-nums; font-size: 12.5px; }
.flag { font-size: 11px; font-weight: 700; color: var(--warn); background: var(--warn-bg); border-radius: 4px; padding: 1px 6px; }
.flag::before { content: "! "; }
.free-label { display: flex; align-items: center; gap: 8px; font: 800 24px/1 var(--display); font-stretch: 85%; color: var(--free); }
.free-mark { width: 12px; height: 12px; border-radius: 50%; background: var(--free); box-shadow: 0 0 0 4px color-mix(in srgb, var(--free) 22%, transparent); }
.meter { height: 10px; border-radius: 5px; background: var(--surface-2); box-shadow: inset 0 0 0 1px var(--line); display: flex; gap: 2px; overflow: hidden; }
.tile.free .meter { background: var(--surface); }
.seg { background: var(--c); min-width: 2px; }
.seg:first-child { border-radius: 5px 0 0 5px; }
.meter-legend { display: flex; gap: 12px; justify-content: space-between; align-items: baseline; font-size: 12.5px; color: var(--ink-2);
  font-variant-numeric: tabular-nums; margin-top: -4px; }
.meter-legend b { color: var(--ink); }
.util-num { font: 700 18px/1 var(--display); color: var(--ink); }
.util-num small { font-size: 12px; }
.spark { height: 24px; flex: 1; min-width: 0; align-self: center; }
.spark-base { stroke: var(--line-2); stroke-width: 1; }
.spark-area { fill: var(--accent); opacity: 0.16; }
.spark-line { fill: none; stroke: var(--accent); stroke-width: 1.5; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
.spark-base { vector-effect: non-scaling-stroke; }
.spark-empty { font-size: 11px; color: var(--muted); text-align: center; }
.small { margin: -4px 0 0; font-size: 11.5px; color: var(--muted); }
.task { border-top: 1px solid var(--line); padding-top: 8px; display: grid; gap: 3px; min-width: 0; }
.tile.free .task { border-color: color-mix(in srgb, var(--free) 25%, var(--line)); }
.task.none { color: var(--muted); font-size: 12.5px; }
.task-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 12.5px; }
.until { margin-left: auto; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.tag { font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 700; color: var(--ink-2);
  border: 1px solid var(--line-2); border-radius: 4px; padding: 0 5px; }
.tag.excl { color: var(--crit); border-color: color-mix(in srgb, var(--crit) 45%, transparent); }
.tag.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 45%, transparent); }
.task-text { margin: 0; font-size: 12.5px; color: var(--ink-2); overflow-wrap: anywhere; }

/* calendar */
.cal-card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 10px 0 6px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 12.5px; color: var(--ink-2); padding: 0 14px 10px; }
.legend > span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 26px; height: 14px; border-radius: 3px; --c: var(--ink-2); display: inline-block; }
.cal-scroll { overflow-x: auto; overscroll-behavior-x: contain; }
.cal-scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.cal { position: relative; padding-right: 14px; padding-bottom: 24px; }
.cal-axis, .cal-row, .cal-under, .cal-over { display: grid; grid-template-columns: var(--gpucol) 1fr; }
.cal-gpu { position: sticky; left: 0; z-index: 3; background: var(--surface); font: 700 12.5px/1 var(--body);
  display: flex; align-items: center; padding-left: 14px; color: var(--ink-2); }
.cal-track { position: relative; }
.cal-axis .cal-track { height: 40px; }
.day-label { position: absolute; top: 2px; font-weight: 700; font-size: 12.5px; padding-left: 6px; white-space: nowrap; }
.day-label.first { padding-left: 4px; }
.tick { position: absolute; bottom: 0; height: 8px; border-left: 1px solid var(--line-2); }
.tick span { position: absolute; bottom: 9px; left: -8px; font: 10.5px var(--mono); color: var(--muted); }
.tick.major span { color: var(--ink-2); }
.cal-axis .day-line { position: absolute; top: 0; bottom: 0; border-left: 1px solid var(--line-2); }
.cal-body { position: relative; }
.cal-under, .cal-over { position: absolute; inset: 0; pointer-events: none; }
.cal-under .cal-gpu, .cal-over .cal-gpu { background: transparent; }
.cal-over { z-index: 2; }
.night { position: absolute; top: 0; bottom: 0; background: var(--night); }
.past { position: absolute; left: 0; top: 0; bottom: 0; background: var(--past); }
.cal-under .day-line { position: absolute; top: 0; bottom: 0; border-left: 1px solid var(--line-2); }
.cal-row { position: relative; border-top: 1px solid var(--line); }
.cal-row:last-of-type { border-bottom: 1px solid var(--line); }
.cal-row .cal-track { height: calc(var(--lanes) * var(--lane) + 8px); margin-block: 4px; margin-bottom: 0; }
.blk { position: absolute; height: calc(var(--lane) - 4px); border-radius: 5px; overflow: hidden; display: flex; align-items: center;
  padding: 0 7px; font-size: 12px; color: var(--ink); white-space: nowrap; cursor: default; }
.blk-text { overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.blk .t { color: var(--ink-2); font-variant-numeric: tabular-nums; }
.blk.book { background: color-mix(in srgb, var(--c) 24%, var(--surface)); box-shadow: inset 0 0 0 1.5px var(--c); }
.blk.live { background: repeating-linear-gradient(135deg, color-mix(in srgb, var(--c) 30%, var(--surface)) 0 6px,
  color-mix(in srgb, var(--c) 14%, var(--surface)) 6px 12px); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--c) 60%, transparent); }
.blk.unreg { background: linear-gradient(90deg, color-mix(in srgb, var(--c) 26%, var(--surface)) 0 65%, transparent);
  box-shadow: inset 2px 0 0 var(--c); border-radius: 0 5px 5px 0; }
.blk.clash { outline: 2px solid var(--crit); outline-offset: -1px; }
.sw.book { background: color-mix(in srgb, var(--c) 24%, var(--surface)); box-shadow: inset 0 0 0 1.5px var(--c); }
.sw.live { background: repeating-linear-gradient(135deg, color-mix(in srgb, var(--c) 30%, var(--surface)) 0 4px,
  color-mix(in srgb, var(--c) 12%, var(--surface)) 4px 8px); box-shadow: inset 0 0 0 1px var(--c); }
.sw.unreg { background: linear-gradient(90deg, color-mix(in srgb, var(--c) 30%, var(--surface)), transparent); box-shadow: inset 2px 0 0 var(--c); }
.sw.clash { outline: 2px solid var(--crit); outline-offset: -1px; background: var(--surface); }
.sw.night { background: var(--night); box-shadow: inset 0 0 0 1px var(--line); }
.now-line { position: absolute; top: -4px; bottom: -4px; border-left: 2px solid var(--now); }
.now-line span { position: absolute; bottom: -20px; left: -1px; transform: translateX(-50%); background: var(--now); color: var(--surface);
  font: 700 10.5px/1 var(--mono); padding: 3px 6px; border-radius: 3px; white-space: nowrap; }
.cal-body { margin-top: 0; }
.scroll-hint { display: none; font-size: 12px; color: var(--muted); padding: 6px 14px 0; }
.scroll-hint { display: block; }
.empty { color: var(--ink-2); background: var(--surface); border: 1px dashed var(--line-2); border-radius: 10px; padding: 14px 16px; margin: 0; }
.table-scroll { overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 700; }
td .dot { margin-right: 6px; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }

/* how-to */
.howto { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
@media (max-width: 900px) { .howto { grid-template-columns: minmax(0, 1fr); } }
.cmd { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; display: grid; gap: 8px; min-width: 0; }
.cmd h3 { margin: 0; font: 700 15px/1.2 var(--body); }
.cmd p { margin: 0; color: var(--ink-2); font-size: 12.5px; }
.cmd-line { display: flex; gap: 8px; align-items: stretch; min-width: 0; }
.cmd pre { margin: 0; flex: 1; min-width: 0; overflow-x: auto; background: var(--surface-2); border: 1px solid var(--line);
  border-radius: 6px; padding: 8px 10px; font: 12px/1.5 var(--mono); white-space: pre-wrap; overflow-wrap: anywhere; }
.copy { font: 600 12px var(--body); border: 1px solid var(--line-2); background: var(--surface); color: var(--ink); border-radius: 6px;
  padding: 0 10px; cursor: pointer; }
.copy:hover { background: var(--surface-2); }
.copy:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.path { font-size: 12.5px; color: var(--ink-2); margin: 12px 0 0; overflow-wrap: anywhere; }
@media (prefers-reduced-motion: no-preference) { .copy { transition: background 0.15s; } }

/* web mode: the booking form, drag-to-book on the calendar, Remove in the table */
.bookform { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px; display: grid; gap: 14px; }
.f-row { display: flex; flex-wrap: wrap; gap: 12px 18px; align-items: flex-end; }
.fld { display: grid; gap: 5px; min-width: 0; }
.fld-label { font-size: 11.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-2); }
.fld input { font: 14px/1.2 var(--body); color: var(--ink); background: var(--surface-2); border: 1px solid var(--line-2);
  border-radius: 6px; padding: 8px 10px; min-width: 0; max-width: 100%; }
.fld input:focus-visible, .rm-name:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; border-color: var(--accent); }
.fld.name input { width: 15em; }
.fld.grow { flex: 1 1 16em; }
.fld.grow input { width: 100%; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.gchip { position: relative; }
.gchip input { position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }
.gchip span { display: inline-grid; place-items: center; width: 38px; height: 35px; border-radius: 6px; border: 1px solid var(--line-2);
  background: var(--surface-2); font: 700 14px/1 var(--mono); color: var(--ink-2); cursor: pointer; user-select: none; }
.gchip span:hover { border-color: var(--accent); }
.gchip input:checked + span { background: var(--accent); border-color: var(--accent); color: var(--surface); }
.gchip input:focus-visible + span { outline: 2px solid var(--accent); outline-offset: 2px; }
.f-actions { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; }
.primary { font: 700 14px/1 var(--body); background: var(--accent); color: var(--surface); border: 1px solid var(--accent); border-radius: 6px;
  padding: 10px 20px; cursor: pointer; }
.primary:disabled { opacity: 0.6; cursor: progress; }
.primary:focus-visible, .ghost:focus-visible, .rm:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ghost, .rm { font: 600 12.5px/1 var(--body); background: var(--surface); color: var(--ink); border: 1px solid var(--line-2); border-radius: 6px;
  padding: 7px 11px; cursor: pointer; }
.ghost:hover, .rm:hover { background: var(--surface-2); }
.rm.danger { color: var(--surface); background: var(--crit); border-color: var(--crit); }
.f-hint { color: var(--ink-2); font-size: 12.5px; font-variant-numeric: tabular-nums; }
.result { border-radius: 8px; padding: 10px 12px; font-size: 13px; display: grid; gap: 4px; overflow-wrap: anywhere; }
.result.ok { background: var(--free-bg); border: 1px solid color-mix(in srgb, var(--free) 40%, var(--line)); }
.result.err { background: var(--warn-bg); border: 1px solid color-mix(in srgb, var(--warn) 45%, var(--line)); color: var(--ink); }
.result .warn-line { color: var(--warn); font-weight: 700; }
.result p { margin: 0; }
body[data-web] .cal-row .cal-track { cursor: crosshair; }
.sel { position: absolute; top: 2px; bottom: 2px; z-index: 1; border-radius: 5px; pointer-events: none;
  background: color-mix(in srgb, var(--accent) 22%, transparent); box-shadow: inset 0 0 0 1.5px var(--accent); }
tr.confirm td { background: var(--warn-bg); }
.confirm-box { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 10px; }
.rm-name { font: 13px var(--body); color: var(--ink); background: var(--surface); border: 1px solid var(--line-2); border-radius: 6px;
  padding: 6px 8px; width: 12em; max-width: 100%; }
.rm-err { color: var(--crit); font-weight: 700; }
"""

JS = r"""
function stampAgo() {
  var el = document.getElementById("stamp");
  if (!el) return;
  if (!el.dataset.base) el.dataset.base = el.textContent;
  var mins = Math.round((Date.now() / 1000 - Number(el.dataset.ts)) / 60), t = el.dataset.base;
  if (mins >= 1) t += " · " + (mins < 90 ? mins + " min ago" : Math.round(mins / 60) + " h ago");
  el.classList.toggle("stale", mins > 15);
  el.textContent = t + (mins > 15 ? " (stale)" : "");
}
(function () {
  var sc = document.querySelector(".cal-scroll"), nl = document.querySelector(".now-line");
  if (sc && nl) sc.scrollLeft = Math.max(0, nl.getBoundingClientRect().left - sc.getBoundingClientRect().left - Math.min(160, sc.clientWidth * 0.3));
  stampAgo();
  document.querySelectorAll(".copy").forEach(function (b) {
    b.addEventListener("click", function () {
      var pre = b.parentNode.querySelector("pre"), text = pre.textContent;
      var done = function () { b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1400); };
      var select = function () { var r = document.createRange(); r.selectNodeContents(pre);
        var s = getSelection(); s.removeAllRanges(); s.addRange(r); b.textContent = "Selected"; };
      try { navigator.clipboard.writeText(text).then(done, select); } catch (e) { select(); }
    });
  });
})();
"""


WEB_JS = r"""
(function () {
  var TZ = document.body.dataset.tz || "America/New_York", STEP = 1800;
  var fmt = new Intl.DateTimeFormat("en-CA", {timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
                                              hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
  function wall(t) {  // epoch seconds -> "YYYY-MM-DDTHH:MM" in the board's zone, the form's value format
    var p = {}; fmt.formatToParts(new Date(t * 1000)).forEach(function (x) { p[x.type] = x.value; });
    return p.year + "-" + p.month + "-" + p.day + "T" + p.hour + ":" + p.minute;
  }
  function wallMs(w) { var m = /^(\d+)-(\d+)-(\d+)T(\d+):(\d+)$/.exec(w || ""); return m ? Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5]) : NaN; }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  var $ = function (id) { return document.getElementById(id); };
  var form = $("bookform"), name = $("f-name"), from = $("f-from"), to = $("f-to"), note = $("f-note"), hint = $("f-hint"),
      out = $("f-result"), btn = $("f-submit");
  if (!form) return;
  var chips = Array.prototype.slice.call(form.querySelectorAll("input[name=gpu]"));
  var saved = store("agent-bus.name"); if (saved) name.value = saved;
  var t = Math.ceil(Date.now() / 1000 / 3600) * 3600;  // next whole hour; the zone's offset is whole hours
  from.value = wall(t); to.value = wall(t + 2 * 3600);
  function picked() { return chips.filter(function (c) { return c.checked; }).map(function (c) { return +c.value; }); }
  function gtext(gs) {
    var r = [], i = 0;
    while (i < gs.length) { var j = i; while (j + 1 < gs.length && gs[j + 1] === gs[j] + 1) j++; r.push(i === j ? gs[i] : gs[i] + "-" + gs[j]); i = j + 1; }
    return r.join(",");
  }
  function updateHint() {
    var gs = picked(), h = (wallMs(to.value) - wallMs(from.value)) / 3600000;
    hint.textContent = (gs.length ? (gs.length === 1 ? "GPU " : "GPUs ") + gtext(gs) : "no GPU picked") +
      (isFinite(h) ? " · " + (h > 0 ? (Math.round(h * 4) / 4) + " h" : "ends before it starts") : "");
  }
  form.addEventListener("input", updateHint); form.addEventListener("change", updateHint); updateHint();
  function show(ok, lines) {
    out.hidden = false; out.className = "result " + (ok ? "ok" : "err"); out.textContent = "";
    lines.forEach(function (l) { var p = document.createElement("p"); p.textContent = l;
      if (/^warning:/.test(l)) p.className = "warn-line"; out.appendChild(p); });
  }
  function post(path, body) {
    return fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(function (r) { return r.json().catch(function () { return {ok: false, message: "The server answered " + r.status + "."}; }); },
            function () { return {ok: false, message: "Could not reach the board server. Is the ssh tunnel still open?"}; });
  }
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var gs = picked(), problems = [];
    if (!name.value.trim()) problems.push("Type your name: it is shown on the booking.");
    if (!gs.length) problems.push("Pick at least one GPU.");
    if (!(from.value && to.value)) problems.push("Give both a start and an end time.");
    else if (to.value <= from.value) problems.push("The end is before the start.");
    if (problems.length) { show(false, problems); if (!name.value.trim()) name.focus(); return; }
    btn.disabled = true;
    post("/book", {name: name.value.trim(), gpus: gs, from: from.value, to: to.value, note: note.value})
      .then(function (r) {
        btn.disabled = false;
        if (r.ok) { store("agent-bus.name", name.value.trim()); show(true, r.lines || [r.message]); refresh(); }
        else show(false, [r.message]);
      });
  });

  // drag (or click) across empty calendar rows to fill the form
  var drag = null;
  function geom(ev) {
    var cal = document.querySelector(".cal"); if (!cal) return null;
    var rows = Array.prototype.slice.call(cal.querySelectorAll(".cal-row")), row = null;
    rows.forEach(function (r) { var b = r.getBoundingClientRect(); if (ev.clientY >= b.top && ev.clientY < b.bottom) row = r; });
    var tr = rows[0].querySelector(".cal-track").getBoundingClientRect();
    var t0 = +cal.dataset.t0, t1 = +cal.dataset.t1, f = Math.min(1, Math.max(0, (ev.clientX - tr.left) / tr.width));
    return {gpu: row ? +row.dataset.gpu : null, t: t0 + f * (t1 - t0), t0: t0, t1: t1, rows: rows};
  }
  function paint(d) {
    document.querySelectorAll(".sel").forEach(function (e) { e.remove(); });
    var lo = Math.min(d.g0, d.g1), hi = Math.max(d.g0, d.g1), a = d.a, b = d.b;
    d.rows.forEach(function (r) {
      var g = +r.dataset.gpu; if (g < lo || g > hi) return;
      var s = document.createElement("div"); s.className = "sel";
      s.style.left = (100 * (a - d.t0) / (d.t1 - d.t0)) + "%"; s.style.width = (100 * (b - a) / (d.t1 - d.t0)) + "%";
      r.querySelector(".cal-track").appendChild(s);
    });
  }
  function range(d) {
    var a = Math.min(d.ta, d.tb), b = Math.max(d.ta, d.tb);
    a = Math.floor(a / STEP) * STEP;
    b = d.moved ? Math.ceil(b / STEP) * STEP : a + 2 * 3600;
    if (b - a < STEP) b = a + STEP;
    d.a = a; d.b = b;
  }
  document.addEventListener("pointerdown", function (ev) {
    if (ev.button !== 0 || !ev.target.closest(".cal-row .cal-track") || ev.target.closest(".blk")) return;
    var g = geom(ev); if (!g || g.gpu === null) return;
    drag = {g0: g.gpu, g1: g.gpu, ta: g.t, tb: g.t, t0: g.t0, t1: g.t1, rows: g.rows, x: ev.clientX, y: ev.clientY, moved: false,
            touch: ev.pointerType !== "mouse"};
    if (!drag.touch) ev.preventDefault();
  });
  document.addEventListener("pointermove", function (ev) {
    if (!drag || drag.touch) return;
    var g = geom(ev); if (!g) return;
    if (Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) > 6) drag.moved = true;
    if (g.gpu !== null) drag.g1 = g.gpu;
    drag.tb = g.t; range(drag); paint(drag);
  });
  document.addEventListener("pointercancel", function () { drag = null; document.querySelectorAll(".sel").forEach(function (e) { e.remove(); }); });
  document.addEventListener("pointerup", function (ev) {
    if (!drag) return;
    var d = drag; drag = null;
    if (d.touch && Math.abs(ev.clientX - d.x) + Math.abs(ev.clientY - d.y) > 10) return;  // a scroll, not a tap
    range(d); paint(d);
    var lo = Math.min(d.g0, d.g1), hi = Math.max(d.g0, d.g1);
    chips.forEach(function (c) { c.checked = +c.value >= lo && +c.value <= hi; });
    from.value = wall(d.a); to.value = wall(d.b); updateHint();
    out.hidden = true;
    var calm = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    form.scrollIntoView({behavior: calm ? "auto" : "smooth", block: "nearest"});
    (name.value.trim() ? note : name).focus({preventScroll: true});
  });

  // Remove: type the booking's name to confirm (an honor system, like the names themselves)
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("button"); if (!b) return;
    var tr = b.closest("tr");
    if (b.classList.contains("rm") && b.dataset.id) {  // the row's Remove (the confirm button is also .rm)
      document.querySelectorAll("tr.confirm").forEach(function (e) { e.remove(); });
      var c = document.createElement("tr"); c.className = "confirm";
      var td = document.createElement("td"); td.colSpan = tr.children.length;
      td.innerHTML = '<div class="confirm-box"><span>Type the name on this booking to remove it:</span>' +
        '<input class="rm-name" aria-label="Name on the booking" autocomplete="off">' +
        '<button type="button" class="rm danger rm-go">Remove booking</button><button type="button" class="ghost rm-cancel">Cancel</button>' +
        '<span class="rm-err" role="status"></span></div>';
      td.dataset.id = b.dataset.id; td.dataset.name = b.dataset.name;
      c.appendChild(td); tr.after(c); td.querySelector(".rm-name").focus();
    } else if (b.classList.contains("rm-cancel")) { tr.remove(); }
    else if (b.classList.contains("rm-go")) {
      var td2 = b.closest("td"), typed = td2.querySelector(".rm-name").value.trim(), err = td2.querySelector(".rm-err");
      if (typed.toLowerCase() !== td2.dataset.name.trim().toLowerCase()) { err.textContent = "That is not the name on this booking."; return; }
      b.disabled = true;
      post("/unbook", {id: td2.dataset.id, name: typed}).then(function (r) {
        b.disabled = false;
        if (r.ok) { tr.remove(); show(true, r.lines || [r.message]); refresh(); } else err.textContent = r.message;
      });
    }
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && ev.target.classList.contains("rm-name")) { ev.preventDefault(); ev.target.closest("td").querySelector(".rm-go").click(); }
  });

  // refresh the page's data in place every minute, leaving the form as it is
  function refresh() {
    if (drag) return Promise.resolve();
    return fetch(location.pathname, {cache: "no-store"}).then(function (r) { if (!r.ok) throw r.status; return r.text(); }).then(function (txt) {
      var doc = new DOMParser().parseFromString(txt, "text/html");
      document.querySelectorAll("[data-refresh]").forEach(function (el) {
        var k = el.dataset.refresh, n = doc.querySelector('[data-refresh="' + k + '"]');
        if (!n || el.querySelector("tr.confirm")) return;
        var sc = el.querySelector(".cal-scroll"), left = sc ? sc.scrollLeft : null;
        el.replaceWith(n);
        var s2 = n.querySelector(".cal-scroll"); if (s2 && left !== null) s2.scrollLeft = left;
      });
      stampAgo();
    }).catch(function () {
      var el = document.getElementById("stamp");
      if (el) { el.classList.add("stale"); el.textContent = el.dataset.base + " · could not refresh, retrying"; }
    });
  }
  setInterval(refresh, 60000);
  setInterval(stampAgo, 30000);
})();
"""


def book_form():
    chips = "".join(f'<label class="gchip"><input type="checkbox" name="gpu" value="{i}" id="f-g{i}"><span>{i}</span></label>'
                    for i in range(8))
    return f'''<section aria-labelledby="h-form"><h2 id="h-form">Book GPUs <small>information only: nothing is stopped because of a
  booking · drag across empty calendar rows to fill this in</small></h2>
<form id="bookform" class="bookform" novalidate>
  <div class="f-row">
    <label class="fld name"><span class="fld-label">Your name</span>
      <input id="f-name" name="name" type="text" required maxlength="32" autocomplete="nickname" spellcheck="false" placeholder="shown on the booking"></label>
    <div class="fld" role="group" aria-labelledby="f-gpus-label"><span class="fld-label" id="f-gpus-label">GPUs</span>
      <div class="chips">{chips}</div></div>
  </div>
  <div class="f-row">
    <label class="fld"><span class="fld-label">From (ET)</span><input id="f-from" name="from" type="datetime-local" step="900" required></label>
    <label class="fld"><span class="fld-label">To (ET)</span><input id="f-to" name="to" type="datetime-local" step="900" required></label>
    <label class="fld grow"><span class="fld-label">Note</span>
      <input id="f-note" name="note" type="text" maxlength="200" placeholder="what it is for"></label>
  </div>
  <div class="f-actions"><button type="submit" class="primary" id="f-submit">Book</button><span class="f-hint" id="f-hint"></span></div>
  <div id="f-result" class="result" role="status" aria-live="polite" hidden></div>
</form></section>'''


def render(data, web=None):
    """The page. web=dict(owner=<the server's unix user>) adds the booking form, Remove buttons, drag-to-book and a
    refresh every 60 s (board_server.py); without it the page is static."""
    now, host = data["now"], data.get("host") or "GPU host"
    board = data.get("board") or BOARD
    names = {g.get("name") for g in data["gpus"]}
    model = names.pop().replace("NVIDIA ", "").split()[0] if len(names) == 1 and None not in names else "GPU"
    entries = [e for e in data["entries"] if e.get("gpus")]
    users = {u for g in data["gpus"] for u, _ in occupants(g)} | {e["user"] for e in entries} | {who(b) for b in data["bookings"]}
    slot = colours(users)
    span_s = history_span(data["usage"], now)
    tiles = "".join(tile(g, entries, slot, data["usage"], now, span_s) for g in sorted(data["gpus"], key=lambda g: g["gpu"]))
    nfree = sum(1 for g in data["gpus"]
                if not [1 for _, mb in occupants(g) if mb >= SMALL_MB] and not [e for e in entries if g["gpu"] in e["gpus"]])
    unreg = sorted({u for g in data["gpus"] for u, mb in occupants(g) if mb >= SMALL_MB
                    and u not in {e["user"] for e in entries if g["gpu"] in e["gpus"]}})
    upcoming = [b for b in data["bookings"] if b["end"] > now]
    stamp = datetime.fromtimestamp(now, TZ)
    bus = f"{board}/bin/bus.py"
    cmds = [("Book GPUs", "Say when you need them, in Eastern time. A booking is information only: nothing is stopped because of it, and an overlap prints a warning naming who.",
             f'python3 {bus} book --gpus 0-3 --from 22:00 --to 01:00 --note "what it is for"'),
            ("Remove a booking", "Only the owner can remove one. The id is in the table above and in the output of book.",
             f"python3 {bus} unbook <id>"),
            ("See the calendar", "Who is on each GPU now and the bookings ahead, in the terminal. Add --days 3 to look further.",
             f"python3 {bus} cal")]
    howto = "".join(f'<div class="cmd"><h3>{esc(t)}</h3><p>{esc(p)}</p><div class="cmd-line"><pre>{esc(c)}</pre>'
                    f'<button class="copy" type="button" id="copy-{i}">Copy</button></div></div>'
                    for i, (t, p, c) in enumerate(cmds))
    legend_c = "--c:var(--ink-2)"
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{esc(host)} GPU board</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..800&family=JetBrains+Mono:wght@400;700&display=swap">
<style>{CSS}</style></head>
<body data-tz="{esc(data.get("tz") or TZ.key)}"{" data-web" if web else ""}><main class="wrap">
<header class="top" data-refresh="top">
  <div><h1>{esc(host)} <span>{len(data["gpus"])} × {esc(model)}</span></h1>
    <p class="sub">Who is on each GPU now, and who has booked which GPUs when · times in Eastern (ET)</p></div>
  <div class="summary">
    <span class="chip free"><b>{nfree}</b> of {len(data["gpus"])} free</span>
    <span class="chip">{len(upcoming)} booking{"" if len(upcoming) == 1 else "s"} ahead</span>
    {f'<span class="chip"><span class="flag">not on board</span> {esc(", ".join(unreg))}</span>' if unreg else ""}
    <span class="stamp" id="stamp" data-ts="{now}">updated {stamp.strftime("%a %b %-d, %H:%M")} ET</span>
  </div>
</header>

<section aria-labelledby="h-now" data-refresh="now"><h2 id="h-now">Now <small>bar: memory by user · line: utilization over the last {span_s / 3600:.0f} h · board entries at the bottom</small></h2>
<div class="tiles">{tiles}</div></section>

<section aria-labelledby="h-cal"><h2 id="h-cal">Calendar <small>last 3 h and the next 72 h, Eastern time · hover a block for details{" · drag across empty rows to book" if web else ""}</small></h2>
<div class="cal-card" data-refresh="cal">
  <div class="legend">
    <span><i class="sw book" style="{legend_c}"></i>booking</span>
    <span><i class="sw live" style="{legend_c}"></i>on the board now, until its expected end</span>
    <span><i class="sw unreg" style="{legend_c}"></i>running, not on the board (end unknown)</span>
    <span><i class="sw clash"></i>overlaps someone else</span>
    <span><i class="sw night"></i>22:00–07:00</span>
  </div>
  {calendar(data, slot, now)}
  <p class="scroll-hint">Scroll the calendar sideways for later days.</p>
</div></section>

{book_form() if web else ""}
<section aria-labelledby="h-book" data-refresh="bookings"><h2 id="h-book">Bookings</h2>{booking_table(data, slot, now, web)}</section>

<section aria-labelledby="h-how"><h2 id="h-how">{"From a terminal" if web else "Book, unbook, view"} <small>run on {esc(host)}</small></h2>
<div class="howto">{howto}</div>
<p class="path">The board lives in <code>{esc(board)}</code>; the tool is <code>{bus}</code>.</p></section>
</main><script>{JS}</script>{f"<script>{WEB_JS}</script>" if web else ""}</body></html>
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["collect", "render"])
    ap.add_argument("input", nargs="?", help="render: the JSON from collect ('-' for stdin)")
    ap.add_argument("-o", "--out", help="where the HTML goes (default stdout)")
    ap.add_argument("--board", help="the board directory (default for collect: the directory above tools/; required with --ssh)")
    ap.add_argument("--ssh", help="run collect on this host over one ssh call, then render here")
    ap.add_argument("--save-json", help="with --ssh: also keep the collected JSON here")
    a = ap.parse_args()
    if a.cmd == "collect":
        print(json.dumps(collect(a.board or BOARD), ensure_ascii=False))
        return
    if a.ssh:
        if not a.board:
            ap.error("--ssh needs --board: the board directory on that host")
        r = subprocess.run(["ssh", a.ssh, f"python3 - collect --board {shlex.quote(a.board)}"], input=Path(__file__).read_text(),
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"collect on {a.ssh} failed ({r.returncode}): {r.stderr.strip()}")
        raw = r.stdout
        if a.save_json:
            Path(a.save_json).write_text(raw)
    elif a.cmd == "render" and a.input:
        raw = sys.stdin.read() if a.input == "-" else Path(a.input).read_text()
    else:
        ap.error("give `collect`, `render IN.json`, or --ssh HOST")
    page = render(json.loads(raw))
    if a.out:
        Path(a.out).write_text(page)
    else:
        sys.stdout.write(page)


if __name__ == "__main__":
    main()
