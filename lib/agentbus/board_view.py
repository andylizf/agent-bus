"""Board layout ported from tools/board_page.py at bff1824."""

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from . import core

TZ = ZoneInfo("America/New_York")
ROBOTS = "noindex, nofollow, noarchive"
HISTORY = 24 * 3600
PAST, AHEAD = 3 * 3600, 72 * 3600
SLOTS = 8


def esc(s):
    return html.escape(str(s), quote=True)


def gib(mb):
    return mb / 1024


def clock(t, day=False):
    try:
        d = datetime.fromtimestamp(t, TZ)
    except (OSError, OverflowError, ValueError):
        return "unknown"
    return d.strftime("%a %H:%M") if day else d.strftime("%H:%M")


def span(a, b, now):
    """'21:00 - 01:00' with the weekday on each end that is not today."""
    today = datetime.fromtimestamp(now, TZ).date()
    if a < now - 7 * 86400 or b > now + 7 * 86400:
        return f"{clock(a, True)} – {clock(b, True)}"
    start = clock(a, datetime.fromtimestamp(a, TZ).date() != today)
    end = clock(b, datetime.fromtimestamp(b, TZ).date() != today)
    return f"{start} – {end}" if " " in start or " " in end else f"{start}–{end}"


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


def occupants(g):
    """[(user, MiB)] on a GPU, highest memory first."""
    return sorted(g["users"].items(), key=lambda item: (-item[1], item[0]))


def history_span(usage, now):
    oldest = min((t for samples in usage.values() for t, _ in samples), default=now)
    return min(HISTORY, max(3600, now - oldest))


def spark(usage, gpu, now, span_s):
    """An SVG area of utilization over the last span_s seconds, in 48 bins."""
    W, H, n = 100, 24, 48
    bins = [[] for _ in range(n)]
    for stamp, util in usage.get(gpu, ()):
        k = int((stamp - (now - span_s)) * n // span_s)
        if 0 <= k < n:
            bins[k].append(util)
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
    mine = [e for e in entries if g["gpu"] in e["gpus"]]
    board_users = {e["user"] for e in mine}
    state, label = ("busy", "In use") if occ else ("claimed", "Claimed") if mine else ("free", "Free")
    if not g["available"]:
        state, label = "unknown", "Unavailable"
    total = g["total_mb"]
    bar = "" if not total else "".join(
        f'<span class="seg" style="width:{100 * mb / total:.2f}%;--c:var(--u{slot[u]})" '
        f'title="{esc(u)}: {gib(mb):.1f} GiB"></span>' for u, mb in occ if mb)
    who = []
    for u, mb in occ:
        flag = "" if u in board_users else '<span class="flag">Not on board</span>'
        memory = f"{gib(mb):.1f} GiB" if mb else ""
        who.append(f'<li class="who"><span class="dot" style="--c:var(--u{slot[u]})"></span>'
                   f'<span class="name">{esc(u)}</span><span class="mem">{memory}</span>{flag}</li>')
    tasks = []
    for e in sorted(mine, key=lambda entry: not entry["protected"]):
        kind = e["class"]
        display = "Scheduled" if kind == "scheduled" else "Backfill"
        if kind == "scheduled" and not e["protected"]:
            display = "Scheduled · backfill now"
        treatment = "scheduled" if e["protected"] else "backfill"
        tags = [f'<span class="tag {treatment}">{display}</span>']
        if e["state"] != "running":
            tags.append(f'<span class="tag warn">{esc(e["state"])}</span>')
        if e["idle"]:
            tags.append('<span class="tag">Idle</span>')
        if e["occupancy"] != "live":
            tags.append('<span class="tag warn">Heartbeat unknown</span>')
        if e["protected"] and e["started"] and e["until"]:
            until = span(e["started"], e["until"], now)
        else:
            until = f'until {clock(e["until"], e["until"] - now > 12 * 3600)}' if e["until"] else ""
        note = f'<p class="task-text">{esc(e["note"])}</p>' if e["note"] else ""
        tasks.append(f'<div class="task"><div class="task-meta"><span class="dot" style="--c:var(--u{slot[e["user"]]})"></span>'
                     f'<b>{esc(e["user"])}</b> {"".join(tags)}<span class="until">{until}</span></div>{note}</div>')
    util = "–" if g["util"] is None else f'{g["util"]}<small>%</small>'
    mem = f'<b>{gib(g["mem_mb"]):.0f}</b> / {gib(total):.0f} GiB' if total else "Memory unavailable"
    status = " has-scheduled" if any(e["protected"] for e in mine) else ""
    return f'''<article class="tile {state}{status}" aria-label="GPU {g["gpu"]}, {label}">
  <header class="tile-head"><h3>GPU {g["gpu"]}</h3><span class="state-pill {state}">{label}</span></header>
  <ul class="whos">{"".join(who)}</ul>
  <div class="gauge">
    <div class="meter" role="img" aria-label="GPU {g["gpu"]} memory by user">{bar}</div>
    <div class="meter-legend"><span class="memtext">{mem}</span>{spark(usage, g["gpu"], now, span_s)}
      <span class="util"><span class="util-num">{util}</span> util</span></div>
  </div>
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
    rows = {g["gpu"]: [] for g in data["gpus"]}
    for b in data["bookings"]:
        for g in b["gpus"]:
            if g in rows and b["end"] > t0 and b["start"] < t1:
                rows[g].append(dict(kind="book", a=b["start"], b=b["end"], user=b["user"], state=b["state"],
                                    label=b["user"], note=b.get("note", "")))
    for e in data["entries"]:
        end = max(e["until"] or now, now)
        start = min(e["started"] or now, now)
        kind = "scheduled" if e["protected"] else "backfill"
        for g in e["gpus"]:
            if g in rows:
                rows[g].append(dict(kind=kind, a=start, b=end, user=e["user"], label=e["user"],
                                    note=e["note"], e=e))
    for g in data["gpus"]:
        board = {e["user"] for e in data["entries"] if g["gpu"] in e["gpus"]}
        for u, mb in occupants(g):
            if u not in board:
                rows[g["gpu"]].append(dict(kind="unreg", a=now, b=now + 3 * 3600,
                                           user=u, label=u, mb=mb))
    # Off-board use is observed at now. Its faded label reserves drawing space only.
    def overlaps(a, b):
        if a["kind"] == "unreg":
            return b["a"] <= now < b["b"]
        if b["kind"] == "unreg":
            return a["a"] <= now < a["b"]
        return a["a"] < b["b"] and b["a"] < a["b"]

    for items in rows.values():
        for it in items:
            it["clash"] = [o for o in items if overlaps(it, o)
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
            state = ""
            if it["kind"] == "book":
                state = it["state"]
                label = f'{state.title()} booking'
                detail = span(it["a"], it["b"], now)
            elif it["kind"] in ("scheduled", "backfill"):
                e = it["e"]
                label = "Scheduled" if e["protected"] else "Backfill"
                if e["class"] == "scheduled" and not e["protected"]:
                    label = "Scheduled · backfill now"
                detail = span(e["started"], e["until"], now) if e["protected"] and e["started"] and e["until"] else (
                    f'Expected end {clock(e["until"], True)}' if e["until"] else "End unknown")
                if e["occupancy"] != "live":
                    detail += "\nHeartbeat unknown"
            else:
                label = "Not on board"
                detail = f'{gib(it["mb"]):.1f} GiB on GPU {gi}, end unknown'
            tip = f'{it["user"]} · {label}\n{detail}'
            if it.get("note"):
                tip += "\n" + it["note"]
            block_label = state.title() if it["kind"] == "book" else label
            text = f'<b>{esc(it["user"])}</b> <span class="t">{esc(block_label)}</span>'
            if it["kind"] == "scheduled":
                text += f' <span class="t">{esc(detail.splitlines()[0])}</span>'
            if it["clash"]:
                tip += "\noverlaps: " + ", ".join(sorted({o["user"] for o in it["clash"]}))
            blocks.append(f'<div class="blk {it["kind"]} {state}{" clash" if it["clash"] else ""}" '
                          f'style="left:{a:.3f}%;width:{max(b - a, 0.4):.3f}%;top:calc({it["lane"]} * var(--lane));{colour}" '
                          f'title="{esc(tip)}"><span class="blk-text">{text}</span></div>')
        out.append(f'<div class="cal-row" data-gpu="{gi}" style="--lanes:{n}"><div class="cal-gpu">GPU {gi}</div>'
                   f'<div class="cal-track">{"".join(blocks)}</div></div>')
    return f'''<div class="cal-scroll" tabindex="0" aria-label="Calendar, scrolls sideways">
  <div class="cal" data-t0="{t0:.0f}" data-t1="{t1:.0f}" style="min-width:calc(var(--gpucol) + {(t1 - t0) / 3600:.0f} * var(--hour))">
    <div class="cal-axis"><div class="cal-gpu"></div><div class="cal-track">{"".join(days)}{"".join(lines)}{"".join(ticks)}
      <div class="now-tick" style="left:{nowx:.3f}%"></div></div></div>
    <div class="cal-body">
      <div class="cal-under"><div class="cal-gpu"></div><div class="cal-track">{"".join(nights)}
        <div class="past" style="width:{nowx:.3f}%"></div>
        {"".join(lines)}
        <div class="now-line" style="left:{nowx:.3f}%"><span>now {clock(now)}</span></div></div></div>
      {"".join(out)}
    </div>
  </div>
</div>'''


def booking_table(bookings, slot, now):
    if not bookings:
        return '<p class="empty">No bookings in this window.</p>'
    rows = []
    for b in sorted(bookings, key=lambda b: b["start"]):
        name, state = b["user"], b["state"]
        objections = "<br>".join(f'{esc(o["user"])}: {esc(o["reason"])}' for o in b["objections"])
        note = "<br>".join(text for text in (esc(b["note"]), objections) if text)
        close = clock(b["objection_close"], True) if state == "pending" and b["objection_close"] else "–"
        rows.append(f'<tr><td><span class="dot" style="--c:var(--u{slot[name]})"></span>{esc(name)}</td>'
                    f'<td class="num">{core.gpu_text(b["gpus"])}</td>'
                    f'<td class="num">{esc(span(b["start"], b["end"], now))}</td>'
                    f'<td><span class="state-pill {state}">{state.title()}</span></td>'
                    f'<td class="num">{close}</td><td>{note}</td></tr>')
    return ('<div class="table-scroll" tabindex="0" aria-label="Bookings, scrolls sideways">'
            '<table><thead><tr><th>Who</th><th>GPUs</th><th>When (ET)</th><th>State</th><th>Objections close</th>'
            f'<th>Notes</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render(data, snapshot=False):
    now = data["t"]
    stamp = datetime.fromtimestamp(now, TZ)
    gpus = data["gpus"]
    bookings = data["bookings"]
    entries = data["entries"]
    usage = data["usage"]
    users = {u for g in gpus for u in g["users"]} | {e["user"] for e in entries} | {b["user"] for b in bookings}
    slot = colours(users)
    span_s = history_span(usage, now)
    tiles = "".join(tile(g, entries, slot, usage, now, span_s) for g in gpus)
    nfree = sum(g["available"] and not g["users"] and all(g["gpu"] not in e["gpus"] for e in entries) for g in gpus)
    upcoming = sum(b["start"] > now for b in bookings)
    notes = "".join(f'<p class="warn">{esc(message)}</p>' for message in data["notices"])
    mode = "Snapshot" if snapshot else "Live"
    board = f'<span class="muted"> · <code>{esc(data["board"])}</code></span>' if data["board"] else ""
    refresh = "" if snapshot else '<meta http-equiv="refresh" content="60">'
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="{ROBOTS}">{refresh}
<title>{esc(data["host"])} GPU board</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&amp;family=Google+Sans+Code:wght@400;500;600&amp;display=swap">
<style>{CSS}</style></head>
<body><main class="wrap">
<header class="top">
  <p class="eyebrow"><span>GPU board · {mode}</span><time class="stamp" id="stamp" datetime="{stamp.isoformat()}" data-ts="{now}">Updated {stamp.strftime("%a %b %-d, %H:%M")} ET</time></p>
  <h1>{esc(data["host"])} <span>{len(gpus)} × {esc(data["model"])}</span></h1>
  <div class="summary"><span class="chip free"><b>{nfree}</b> of {len(gpus)} free</span>
    <span class="chip"><b>{upcoming}</b> booking{"s" if upcoming != 1 else ""} ahead</span></div>
  <div class="notes" role="status">{notes}</div>
</header>
<section aria-labelledby="h-now"><div class="sec-head"><h2 id="h-now">Now</h2>
  <p>Memory by user · GPU utilization over the last {span_s / 3600:.0f} h</p></div>
<div class="tiles">{tiles}</div></section>
<section aria-labelledby="h-cal"><div class="sec-head"><h2 id="h-cal">Calendar</h2>
  <p>Previous 3 h and next 72 h · Eastern time</p></div>
<div class="cal-card">
  <div class="legend">
    <span><i class="sw sw-scheduled"></i>Scheduled run</span>
    <span><i class="sw sw-backfill"></i>Backfill run</span>
    <span><i class="sw sw-unreg"></i>Not on board · end unknown</span>
    <span><i class="sw sw-book"></i>Booking</span>
    <span><i class="sw sw-clash"></i>Overlap</span>
    <span><i class="sw sw-night"></i>22:00–07:00</span>
  </div>
  {calendar(data, slot, now)}
  <p class="scroll-hint">Scroll sideways for later days.</p>
</div></section>
<section aria-labelledby="h-book"><div class="sec-head"><h2 id="h-book">Bookings</h2><p>{OBJECTION_WINDOW}</p></div>
{booking_table(bookings, slot, now)}</section>
<footer>Agents book GPUs with <a href="https://github.com/andylizf/agent-bus#bookings-as-public-notices"><code>bus book</code></a>{board}.</footer>
</main><script>{JS}</script></body></html>
'''


OBJECTION_WINDOW = (f"Objection window: {core.DEFAULT_LIMITS['default_objection_s'] // 60} min by default, "
                    f"{core.DEFAULT_LIMITS['min_objection_s'] // 60} min minimum.")


CSS = r"""
/* Layout: a white research-notebook page. A date-style eyebrow and a large title, sections opened by a heading on a
   hairline, the 8 GPUs as one ruled 4 x 2 panel (like the baseboard), then a GPU x time calendar and a bookings table. Colour is spent on users and on state only. */
:root {
  --bg: #ffffff; --wash: #f8f9fa; --wash-2: #f1f3f4; --ink: #202124; --ink-2: #3c4043; --muted: #5f6368;
  --line: #dadce0; --line-2: #e8eaed; --accent: #1a73e8; --pill: #d3e3fd; --pill-ink: #041e49;
  --now: #1a73e8; --night: rgba(60, 64, 67, 0.045); --past: rgba(60, 64, 67, 0.06);
  --free: #137333; --free-bg: #f3faf5; --warn: #b05a00; --warn-bg: #fef7e0; --crit: #c5221f; --crit-bg: #fce8e6;
  --scheduled: #184e9c; --scheduled-bg: #e0ebfc; --backfill: #41684d; --backfill-bg: #edf5ee;
  --u1: #2a78d6; --u2: #eb6834; --u3: #1baf7a; --u4: #eda100; --u5: #e87ba4; --u6: #008300; --u7: #6250d6; --u8: #e34948;
  --body: "Hanken Grotesk", "Helvetica Neue", Arial, sans-serif;
  --mono: "Google Sans Code", ui-monospace, "SF Mono", Menlo, monospace;
  --lane: 28px; --gpucol: 64px; --hour: 36px; --r: 8px;
}
@media (prefers-color-scheme: dark) { :root {
  --bg: #131314; --wash: #1b1c1d; --wash-2: #232425; --ink: #e3e3e3; --ink-2: #c4c7c5; --muted: #8e918f;
  --line: #3c3f41; --line-2: #2a2b2d; --accent: #a8c7fa; --pill: #0b3a82; --pill-ink: #d3e3fd;
  --now: #a8c7fa; --night: rgba(168, 199, 250, 0.045); --past: rgba(0, 0, 0, 0.28);
  --free: #6dd58c; --free-bg: #16231a; --warn: #f6ae5a; --warn-bg: #2d2214; --crit: #f28b82; --crit-bg: #3a1d1b;
  --scheduled: #b7d2ff; --scheduled-bg: #243a59; --backfill: #92b99c; --backfill-bg: #202d24;
  --u1: #3987e5; --u2: #d95926; --u3: #199e70; --u4: #c98500; --u5: #d55181; --u6: #2a9a2a; --u7: #9085e9; --u8: #e66767;
  color-scheme: dark; } }
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body { background: var(--bg); color: var(--ink); font: 15px/1.5 var(--body); margin: 0; -webkit-font-smoothing: antialiased; }
.wrap > * { min-width: 0; }
.wrap { max-width: 1240px; margin: 0 auto; padding-inline: 24px; padding-block: 44px 72px; display: grid; gap: 52px; }
@media (max-width: 600px) { .wrap { padding-inline: 16px; padding-block: 24px 48px; gap: 40px; } }
code, .mono { font-family: var(--mono); font-size: 0.86em; }
.muted { color: var(--muted); }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--c); flex: none; }

/* header: a date-style line, the title, one sentence, then the summary on a hairline */
.top { display: grid; gap: 10px; }
.eyebrow { margin: 0; display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: baseline; font-size: 14px; font-weight: 500; color: var(--ink-2); }
.eyebrow > span:first-child { font-weight: 600; color: var(--ink); }
.top h1 { font: 600 clamp(34px, 5.2vw, 46px)/1.1 var(--body); letter-spacing: -0.01em; margin: 6px 0 0; text-wrap: balance; }
.top h1 span { color: var(--muted); font-weight: 500; }
.summary { display: flex; flex-wrap: wrap; gap: 8px 28px; align-items: baseline; margin-top: 14px; padding-bottom: 14px;
  border-bottom: 1px solid var(--line); }
.chip { display: inline-flex; align-items: baseline; gap: 6px; font-size: 15px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
.chip b { font-weight: 600; color: var(--ink); font-size: 17px; }
.chip.free b { color: var(--free); }
.stamp { font-variant-numeric: tabular-nums; }
.stamp.stale { color: var(--warn); font-weight: 600; }

/* section heads: heading, one grey line, a hairline */
.sec-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 16px; padding-bottom: 12px; margin-bottom: 20px;
  border-bottom: 1px solid var(--line); }
.sec-head h2 { font: 600 26px/1.2 var(--body); letter-spacing: -0.01em; margin: 0; }
.sec-head p { margin: 0; color: var(--muted); font-size: 14px; }

/* status chips: small outlined labels; tags: small grey filled labels */
.state-pill, .flag { display: inline-flex; align-items: center; font-size: 11.5px; font-weight: 600; line-height: 1; padding: 3px 7px;
  border-radius: 4px; border: 1px solid currentColor; white-space: nowrap; }
.state-pill { margin-left: auto; color: var(--ink-2); }
.state-pill.free { color: var(--free); }
.state-pill.claimed { color: var(--muted); border-style: dashed; }
.flag { color: var(--crit); background: var(--crit-bg); }
.tag { font-size: 11.5px; font-weight: 600; color: var(--ink-2); background: var(--wash-2); border-radius: 4px; padding: 1px 6px; }
.tag.excl { color: var(--crit); background: var(--crit-bg); }
.tag.warn { color: var(--warn); background: var(--warn-bg); }

/* GPU tiles: one ruled panel, 4 x 2 */
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; background: var(--line);
  border: 1px solid var(--line); border-radius: var(--r); overflow: hidden; }
@media (max-width: 1100px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 600px) { .tiles { grid-template-columns: minmax(0, 1fr); } }
.tile { background: var(--bg); padding: 16px 18px 16px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.tile.free { background: var(--free-bg); }
.tile.has-scheduled { box-shadow: inset 0 3px var(--scheduled); }
.tile-head { display: flex; align-items: baseline; gap: 8px; }
.tile-head h3 { font: 600 18px/1 var(--body); margin: 0; }
.whos { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; min-height: 24px; }
@media (max-width: 600px) { .whos:empty { display: none; } }
.who { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.who .name { font-weight: 600; font-size: 16px; }
.who .mem { color: var(--ink-2); font-variant-numeric: tabular-nums; font-size: 13px; }
.free-label { display: flex; align-items: center; gap: 8px; font: 600 22px/1 var(--body); color: var(--free); }
.free-mark { width: 10px; height: 10px; border-radius: 50%; background: var(--free); }
.gauge { display: grid; gap: 6px; }
.meter { height: 6px; border-radius: 3px; background: var(--wash-2); display: flex; gap: 2px; overflow: hidden; }
.tile.free .meter { background: color-mix(in srgb, var(--free) 12%, var(--bg)); }
.seg { background: var(--c); min-width: 2px; }
.meter-legend { display: flex; gap: 12px; justify-content: space-between; align-items: center; font-size: 13px; color: var(--muted);
  font-variant-numeric: tabular-nums; }
.memtext b { color: var(--ink); font-weight: 600; }
.util { white-space: nowrap; }
.util-num { font: 600 17px/1 var(--body); color: var(--ink); }
.util-num small { font-size: 12px; font-weight: 500; }
.spark { height: 22px; flex: 1; min-width: 0; }
.spark-base { stroke: var(--line); stroke-width: 1; vector-effect: non-scaling-stroke; }
.spark-area { fill: var(--accent); opacity: 0.12; }
.spark-line { fill: none; stroke: var(--accent); stroke-width: 1.5; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
.spark-empty { font-size: 12px; color: var(--muted); text-align: center; }
.task { border-top: 1px solid var(--line-2); padding-top: 10px; display: grid; gap: 4px; min-width: 0; }
.tile.free .task { border-color: color-mix(in srgb, var(--free) 18%, var(--line-2)); }
.task.none { color: var(--muted); font-size: 13px; }
.task-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 13px; }
.task-meta b { font-weight: 600; }
.until { margin-left: auto; color: var(--muted); font-variant-numeric: tabular-nums; }
.task-text { margin: 0; font-size: 13px; color: var(--ink-2); overflow-wrap: anywhere; }

/* calendar: one hairline panel */
.cal-card { border: 1px solid var(--line); border-radius: var(--r); padding: 14px 0 10px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 20px; font-size: 13px; color: var(--ink-2); padding: 0 18px 14px; }
.legend > span { display: inline-flex; align-items: center; gap: 7px; }
.sw { width: 24px; height: 12px; border-radius: 3px; --c: var(--muted); display: inline-block; flex: none; }
.cal-scroll { overflow-x: auto; overscroll-behavior-x: contain; }
.cal-scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.cal { position: relative; padding-right: 18px; padding-bottom: 30px; }
.cal-axis, .cal-row, .cal-under { display: grid; grid-template-columns: var(--gpucol) 1fr; }
.cal-gpu { position: sticky; left: 0; z-index: 3; background: var(--bg); font: 600 13px/1 var(--body);
  display: flex; align-items: center; padding-left: 18px; color: var(--ink-2); white-space: nowrap; }
.cal-track { position: relative; }
.cal-axis .cal-track { height: 44px; }
.day-label { position: absolute; top: 2px; font-weight: 600; font-size: 13px; padding-left: 6px; white-space: nowrap; color: var(--ink); }
.day-label.first { padding-left: 4px; }
.tick { position: absolute; bottom: 0; height: 6px; border-left: 1px solid var(--line); }
.tick span { position: absolute; bottom: 9px; left: -8px; font: 11px var(--mono); color: var(--muted); }
.tick.major span { color: var(--ink-2); }
.cal-axis .day-line { position: absolute; top: 0; bottom: 0; border-left: 1px solid var(--line); }
.now-tick { position: absolute; bottom: 0; height: 10px; border-left: 2px solid var(--now); margin-left: -1px; }
.cal-body { position: relative; }
.cal-under { position: absolute; inset: 0; pointer-events: none; }
.cal-under .cal-gpu { background: transparent; }
.cal-under .night { position: absolute; top: 0; bottom: 0; background: var(--night); }
.cal-under .past { position: absolute; left: 0; top: 0; bottom: 0; background: var(--past); }
.cal-under .day-line { position: absolute; top: 0; bottom: 0; border-left: 1px solid var(--line); }
.cal-row { position: relative; border-top: 1px solid var(--line-2); }
.cal-row:last-of-type { border-bottom: 1px solid var(--line-2); }
.cal-row .cal-track { height: calc(var(--lanes) * var(--lane) + 6px); margin-top: 3px; }
.blk { position: absolute; height: calc(var(--lane) - 4px); border-radius: 4px; overflow: hidden; display: flex; align-items: center;
  padding: 0 8px; font-size: 12.5px; color: var(--ink); white-space: nowrap; cursor: default; }
.blk-text { overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.blk b { font-weight: 600; }
.blk .t { color: var(--ink-2); font-variant-numeric: tabular-nums; }
.blk.book, .sw-book { background: color-mix(in srgb, var(--c) 18%, var(--bg)); box-shadow: inset 0 0 0 1.5px var(--c); }
.blk.scheduled, .sw-scheduled { background: var(--scheduled-bg); color: var(--scheduled);
  box-shadow: inset 0 0 0 1px var(--scheduled), inset 4px 0 var(--scheduled); }
.blk.scheduled .t { color: var(--scheduled); font-weight: 700; }
.blk.unreg, .sw-unreg { background: linear-gradient(90deg, var(--crit-bg) 0 65%, transparent);
  color: var(--crit); box-shadow: inset 2px 0 var(--crit); }
.blk.unreg .t { color: var(--crit); }
.blk.clash:not(.backfill), .sw-clash { outline: 2px solid var(--crit); outline-offset: -1px; }
.sw-night { background: color-mix(in srgb, var(--ink) 9%, transparent); }
.now-line { position: absolute; top: -3px; bottom: -3px; border-left: 2px solid var(--now); margin-left: -1px; }
.now-line span { position: absolute; bottom: -24px; left: 0; transform: translateX(-50%); background: var(--pill); color: var(--pill-ink);
  font: 600 11.5px/1 var(--body); padding: 4px 8px; border-radius: 50px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.scroll-hint { font-size: 13px; color: var(--muted); padding: 4px 18px 0; margin: 0; }
.empty { color: var(--ink-2); margin: 0; padding: 4px 0; }

/* tables: no box, a ruled header, hairline rows */
.table-scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 580px; font-size: 14px; }  /* narrow screens scroll the table, not squeeze it */
th, td { text-align: left; padding: 10px 14px 10px 0; border-bottom: 1px solid var(--line-2); vertical-align: top; }
th { font-size: 13px; font-weight: 600; color: var(--ink-2); border-bottom-color: var(--line); padding-top: 0; }
td .dot { margin-right: 7px; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }


.blk.backfill, .sw-backfill {
  color: var(--backfill); background: var(--backfill-bg);
  border: 1px solid color-mix(in srgb, var(--backfill) 32%, transparent);
}
.blk.backfill .t { color: var(--backfill); }
.blk.book.pending { box-shadow: none; border: 1px dashed var(--c); }
.blk.book.objected, .blk.book.lapsed, .blk.book.invalid, .blk.book.over-budget {
  background: var(--wash); color: var(--ink-2);
}
.state-pill.standing { color: var(--free); }
.tag.scheduled { color: var(--scheduled); background: var(--scheduled-bg); font-weight: 700; }
.tag.backfill { color: var(--backfill); background: var(--backfill-bg); }
.state-pill.pending { color: var(--warn); border-style: dashed; }
.state-pill.objected, .state-pill.conflicting { color: var(--crit); }
.state-pill.lapsed, .state-pill.invalid, .state-pill.over-budget { color: var(--muted); }
.warn { color: var(--warn); }
.tag.warn { color: var(--warn); }
.notes:empty { display: none; }
.notes p { margin: 0; }
.meter-legend > span { flex: none; }
.meter-legend > .spark-empty { flex: 1; }
td { overflow-wrap: anywhere; }
footer { border-top: 1px solid var(--line); padding-top: 12px; color: var(--muted); font-size: 13px; }
footer a { color: var(--accent); }
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
  if (sc && nl) {  // bring "now" into view only when it is not already in the left part; the first day and labels stay whole
    var x = nl.getBoundingClientRect().left - sc.getBoundingClientRect().left;
    if (x > sc.clientWidth * 0.6) sc.scrollLeft = x - Math.min(160, sc.clientWidth * 0.3);
  }
  stampAgo();
  setInterval(stampAgo, 30000);
})();
"""
