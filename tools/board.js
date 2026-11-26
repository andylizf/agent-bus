"use strict";

const HOUR = 3600;
const POLL = 15000;
const CATCH_UP = 27;
const STALE = 120;
const feed = document.querySelector('meta[name="board-feed"]').content;
const snapshot = feed === "./snapshot.json";
const el = id => document.getElementById(id);
const clock = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", hourCycle: "h23", hour: "2-digit", minute: "2-digit",
});
const date = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", dateStyle: "full" });
const day = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric",
});
const fmt = (format, t) => format.format(t * 1000);
const hours = t => fmt(clock, t).slice(0, 2);
const readableTime = (t, now) => (fmt(date, t) === fmt(date, now) ? "" : fmt(day, t) + " ") + fmt(clock, t);
const windowText = (start, end, now) => `${readableTime(start, now)}–${readableTime(end, now)}`;
const gib = (mb, decimals = 1) => (mb / 1024).toFixed(decimals);
const escapes = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = text => String(text).replace(/[&<>"']/g, ch => escapes[ch]);
const grouped = gpu => Object.entries(gpu.users).sort(([a, x], [b, y]) => (y - x) || a.localeCompare(b));
const stateText = state => state[0].toUpperCase() + state.slice(1);
const kindOf = entry => entry.kind === "scheduled" ? "scheduled" : "backfill";
const kindLabel = entry => kindOf(entry) === "scheduled" ? "Scheduled" : "Backfill";

const UNKNOWN = "Unknown user";
const IN_USE_MB = 2048;  // a CUDA context alone stays below this; more memory, or any utilization, is someone's work
const inUse = gpu => Object.keys(gpu.users).length > 0 || gpu.mem_mb > IN_USE_MB || gpu.util > 0;

// A GPU that reads as in use is never Free, whether or not anyone is listed on it.
function gpuStatus(gpu, entries) {
  if (!gpu.available) return ["unknown", "Unavailable"];
  if (!inUse(gpu)) return entries.length ? ["claimed", "Claimed"] : ["free", "Free"];
  const owners = new Set(entries.map(entry => entry.user));
  const users = Object.keys(gpu.users);
  if (!users.some(user => user !== UNKNOWN)) return ["unattributed", "In use, owner unknown"];
  if (users.some(user => !owners.has(user))) return ["offboard", "Not on board"];
  return ["busy", "In use"];
}

// Scheduled owners are prominent, backfill is muted, off-board and unknown use are warnings.
function occupantKind(user, entries) {
  if (user === UNKNOWN) return "unknown";
  const mine = entries.filter(entry => entry.user === user);
  if (!mine.length) return "off";
  return mine.some(entry => kindOf(entry) === "scheduled") ? "scheduled" : "backfill";
}

function colors(data) {
  const users = new Set([...data.gpus.flatMap(gpu => Object.keys(gpu.users)),
    ...data.entries.map(entry => entry.user), ...data.bookings.map(booking => booking.user)]);
  const slots = new Map();
  const taken = new Set();
  for (const user of Array.from(users).sort()) {
    let slot = Array.from(user).reduce((sum, ch, i) => sum + (i + 1) * ch.charCodeAt(0), 0) % 8;
    while (taken.has(slot) && taken.size < 8) slot = (slot + 1) % 8;
    taken.add(slot);
    slots.set(user, `var(--u${slot + 1})`);
  }
  return slots;
}

function tile(gpu, entries, slots, now) {
  const users = grouped(gpu);
  const [state, label] = gpuStatus(gpu, entries);
  const colour = user => user === UNKNOWN ? "var(--crit)" : slots.get(user);
  const flags = { off: '<span class="flag">Not on board</span>', unknown: '<span class="flag">Owner unknown</span>' };
  let occupants = users.map(([user, mb]) => {
    const kind = occupantKind(user, entries);
    return `
    <li class="who ${kind}"><span class="dot" style="--c:${colour(user)}"></span>
      <span class="name">${esc(user)}</span><span class="mem">${mb ? gib(mb) + " GiB" : ""}</span>${flags[kind] || ""}</li>`;
  }).join("");
  if (state === "unattributed" && !users.length) occupants = `
    <li class="who unknown"><span class="dot" style="--c:var(--crit)"></span>
      <span class="name">No process listed</span><span class="flag">Owner unknown</span></li>`;
  let segments = "";
  if (gpu.total_mb) {
    segments = users.filter(([, mb]) => mb).map(([user, mb]) => `
    <span class="seg ${occupantKind(user, entries)}" style="width:${100 * mb / gpu.total_mb}%;--c:${colour(user)}"
      title="${esc(user)}: ${gib(mb)} GiB"></span>`).join("");
    const unlisted = gpu.mem_mb - users.reduce((sum, [, mb]) => sum + mb, 0);
    if (state === "unattributed" && unlisted > IN_USE_MB) segments += `
    <span class="seg unknown" style="width:${100 * unlisted / gpu.total_mb}%;--c:var(--crit)"
      title="No process listed: ${gib(unlisted)} GiB"></span>`;
  }
  const listed = new Set(users.map(([user]) => user));
  const tasks = [...entries].sort((a, b) => kindOf(b).localeCompare(kindOf(a))).map(entry => {
    const kind = kindOf(entry);
    const until = entry.until ? kind === "scheduled" && entry.started
      ? windowText(entry.started, entry.until, now) : `until ${readableTime(entry.until, now)}` : "End unknown";
    // A scheduled run names its owner; a backfill line is the chip and its end, the name is already above.
    const owner = kind === "scheduled" || !listed.has(entry.user)
      ? `<span class="dot" style="--c:${slots.get(entry.user)}"></span><b>${esc(entry.user)}</b>` : "";
    return `<div class="task ${kind}"><div class="task-meta">${owner}<span class="tag ${kind}">${kindLabel(entry)}</span>
      <span class="until">${until}</span></div></div>`;
  }).join("");
  const scheduled = entries.some(entry => kindOf(entry) === "scheduled");
  const role = scheduled ? " has-scheduled" : entries.length ? " backfill-only" : "";
  const used = gpu.total_mb ? `<b>${gib(gpu.mem_mb, 0)}</b> / ${gib(gpu.total_mb, 0)} GiB` : "Memory unavailable";
  const util = gpu.util === null ? "–" : `${Number(gpu.util)}<small>%</small>`;
  return `<article class="tile ${state}${role}" aria-label="GPU ${Number(gpu.gpu)}, ${label}">
    <header class="tile-head"><h3>GPU ${Number(gpu.gpu)}</h3><span class="state-pill ${state}">${label}</span></header>
    <ul class="whos">${occupants}</ul>
    <div class="gauge"><div class="meter" role="img" aria-label="GPU ${Number(gpu.gpu)} memory by user">${segments}</div>
      <div class="meter-legend"><span class="memtext">${used}</span>
        <span class="util"><span class="util-num">${util}</span> util</span></div></div>${tasks}</article>`;
}

function warnings(data, entries) {
  const states = data.gpus.map(g => gpuStatus(g, entries(g.gpu))[0]);
  return [["unattributed", "in use, owner unknown"], ["offboard", "not on board"]].map(([state, text]) => {
    const n = states.filter(s => s === state).length;
    return n ? `<span class="chip alert ${state}"><b>${n}</b> GPU${n === 1 ? "" : "s"} ${text}</span>` : "";
  }).join("");
}

function calendar(data, slots) {
  const now = data.t;
  const t0 = Math.floor(now / HOUR) * HOUR - 3 * HOUR;
  const t1 = t0 + 76 * HOUR;
  const pct = t => 100 * (Math.max(t0, Math.min(t1, t)) - t0) / (t1 - t0);
  const nowX = pct(now);
  const lines = [], days = [], ticks = [], nights = [];
  const at = (cls, t, body = "") => `<div class="${cls}" style="left:${pct(t)}%">${body}</div>`;
  const night = (a, b) => `<div class="night" style="left:${pct(a)}%;width:${pct(b) - pct(a)}%"></div>`;
  const first = Number(hours(t0));
  if (first >= 3) days.push(at("day-label first", t0, fmt(day, t0)));
  if (first < 7) nights.push(night(t0, t0 + (7 - first) * HOUR));
  if (first > 22) nights.push(night(t0, t0 + (31 - first) * HOUR));
  for (let t = t0; t <= t1; t += HOUR) {
    const h = Number(hours(t));
    if (!h) {
      lines.push(at("day-line", t));
      days.push(at("day-label", t, fmt(day, t)));
    } else if (h % 3 === 0 && t - t0 >= 2 * HOUR) {
      ticks.push(at(h % 6 ? "tick" : "tick major", t, `<span>${hours(t)}</span>`));
    }
    if (h === 22) nights.push(night(t, t + 9 * HOUR));
  }

  const rows = data.gpus.map(gpu => {
    const entries = data.entries.filter(entry => entry.gpus.includes(gpu.gpu));
    const owners = new Set(entries.map(entry => entry.user));
    const items = data.bookings.filter(b => b.gpus.includes(gpu.gpu) && b.end > t0 && b.start < t1)
      .map(b => ({ a: b.start, b: b.end, user: b.user, type: "book", state: b.state, label: stateText(b.state),
        detail: windowText(b.start, b.end, now) }));
    for (const entry of entries) {
      const a = Math.min(entry.started || now, now);
      const b = Math.max(entry.until || now, now);
      items.push({ a, b, user: entry.user, type: kindOf(entry), label: kindLabel(entry),
        detail: entry.until ? windowText(a, entry.until, now) : "End unknown" });
    }
    for (const [user, mb] of grouped(gpu)) {
      if (!owners.has(user)) items.push({ a: now, b: now + 3 * HOUR, user, type: "unreg",
        label: user === UNKNOWN ? "Owner unknown" : "Not on board", detail: `${gib(mb)} GiB on GPU ${gpu.gpu}, end unknown` });
    }
    if (gpu.available && inUse(gpu) && !Object.keys(gpu.users).length) items.push({ a: now, b: now + 3 * HOUR,
      user: UNKNOWN, type: "unreg", label: "Owner unknown", detail: `${gib(gpu.mem_mb)} GiB on GPU ${gpu.gpu}, no process listed` });
    const ends = [];
    const blocks = items.sort((a, b) => a.a - b.a || a.b - b.b).map(it => {
      let lane = ends.findIndex(end => end <= it.a);
      if (lane < 0) lane = ends.length;
      ends[lane] = it.b;
      const clash = items.some(other => other.user !== it.user && [it.type, other.type].includes("book") &&
        (it.type === "unreg" ? other.a <= now && now < other.b : other.type === "unreg" ? it.a <= now && now < it.b :
          it.a < other.b && other.a < it.b));
      const title = `${it.user} · ${it.label}\n${it.detail}${clash ? "\nOverlaps a booking or another user" : ""}`;
      const left = pct(it.a);
      const text = it.type === "scheduled" ? `${it.label} ${it.detail}` : it.label;
      const unknown = it.user === UNKNOWN;
      return `<div class="blk ${it.type}${unknown ? " unknown" : ""} ${esc(it.state || "")}${clash ? " clash" : ""}"
        style="left:${left}%;width:${Math.max(0.4, pct(it.b) - left)}%;top:calc(${lane} * var(--lane));--c:${unknown ? "var(--crit)" : slots.get(it.user)}"
        title="${esc(title)}"><span class="blk-text"><b>${esc(it.user)}</b> <span class="t">${esc(text)}</span></span></div>`;
    }).join("");
    return `<div class="cal-row" style="--lanes:${Math.max(1, ends.length)}"><div class="cal-gpu">GPU ${Number(gpu.gpu)}</div>
      <div class="cal-track">${blocks}</div></div>`;
  }).join("");
  return `<div class="cal" style="min-width:calc(var(--gpucol) + 76 * var(--hour))">
    <div class="cal-axis"><div class="cal-gpu"></div><div class="cal-track">
      ${days.join("")}${lines.join("")}${ticks.join("")}${at("now-tick", now)}</div></div>
    <div class="cal-body"><div class="cal-under"><div class="cal-gpu"></div><div class="cal-track">
      ${nights.join("")}<div class="past" style="width:${nowX}%"></div>${lines.join("")}
      ${at("now-line", now, `<span>now ${fmt(clock, now)}</span>`)}</div></div>${rows}</div></div>`;
}

function bookings(data, slots) {
  if (!data.bookings.length) return '<p class="empty">No bookings in this window.</p>';
  const rows = [...data.bookings].sort((a, b) => a.start - b.start).map(b => `<tr>
    <td><span class="dot" style="--c:${slots.get(b.user)}"></span>${esc(b.user)}</td>
    <td class="num">${esc(b.gpus.join(", "))}</td><td class="num">${windowText(b.start, b.end, data.t)}</td>
    <td><span class="state-pill ${esc(b.state)}">${esc(stateText(b.state))}</span></td>
    <td class="num">${b.state === "pending" && b.objection_close ? readableTime(b.objection_close, data.t) : "–"}</td></tr>`).join("");
  return `<div class="table-scroll" tabindex="0" aria-label="Bookings, scrolls sideways">
    <table><thead><tr><th>Who</th><th>GPUs</th><th>When (ET)</th><th>State</th><th>Objections close</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function render(data) {
  const slots = colors(data);
  const entries = gpu => data.entries.filter(entry => entry.gpus.includes(gpu));
  const free = data.gpus.filter(g => gpuStatus(g, entries(g.gpu))[0] === "free").length;
  const upcoming = data.bookings.filter(b => b.start > data.t).length;
  el("gpu-count").textContent = `${data.gpus.length} GPUs`;
  el("summary").innerHTML = `<span class="chip free"><b>${free}</b> of ${data.gpus.length} free</span>
    <span class="chip"><b>${upcoming}</b> booking${upcoming === 1 ? "" : "s"} ahead</span>${warnings(data, entries)}`;
  el("tiles").innerHTML = data.gpus.map(g => tile(g, entries(g.gpu), slots, data.t)).join("");
  el("calendar").innerHTML = calendar(data, slots);
  el("bookings").innerHTML = bookings(data, slots);
  el("board-path").innerHTML = data.board ? ` · <code>${esc(data.board)}</code>` : "";
  for (const id of ["summary", "now-section", "cal-section", "book-section"]) el(id).hidden = false;
}

let shown = null;
let fetching = false;
let caughtUpFor = null;
const age = t => Math.max(0, Math.floor(Date.now() / 1000 - t));
function stamp() {
  if (!shown) return;
  const seconds = age(shown.t);
  const stale = seconds >= STALE;
  const elapsed = seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` :
    `${Math.floor(seconds / 3600)}h`;
  const time = el("stamp");
  time.dateTime = new Date(shown.t * 1000).toISOString();
  time.dataset.ts = shown.t;
  time.textContent = `Updated ${elapsed} ago${stale ? " (stale)" : ""}`;
  time.classList.toggle("stale", stale);
  // Independent collection and poll clocks can leave a fresh sample waiting between polls.
  if (!snapshot && !fetching && seconds >= CATCH_UP && caughtUpFor !== shown.t && el("status").hidden) {
    caughtUpFor = shown.t;
    poll();
  }
}

async function get(url) {
  const response = await fetch(url, { cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error("Board data unavailable");
  const data = await response.json();
  if (!Number.isSafeInteger(data.t) || data.t <= 0 || data.t > Date.now() / 1000 + 30 ||
      !["gpus", "entries", "bookings"].every(k => Array.isArray(data[k]))) throw new Error("Invalid board data");
  return data;
}

function show(data) {
  if (!shown || data.t > shown.t) {
    render(data);
    shown = data;
  }
  stamp();
}

async function poll() {
  if (fetching) return;
  fetching = true;
  try {
    const data = await get(feed);
    if (!snapshot && age(data.t) >= STALE) throw new Error("Live feed is stale");
    show(data);
    el("mode").textContent = snapshot ? "GPU board · Snapshot" : "GPU board · Live";
    el("status").hidden = true;
  } catch {
    if (snapshot) {
      el("status").textContent = "Snapshot unavailable.";
      return;
    }
    try { show(await get("./snapshot.json")); } catch { /* Keep the last good reading. */ }
    el("mode").textContent = shown ? "GPU board · Snapshot" : "GPU board";
    el("status").textContent = shown ? "Live data unavailable. Showing the latest available reading." : "Live data unavailable.";
    el("status").className = "warn";
    el("status").hidden = false;
  } finally {
    fetching = false;
  }
}

poll();
if (!snapshot) setInterval(poll, POLL);
setInterval(stamp, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
