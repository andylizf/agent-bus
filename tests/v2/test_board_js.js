"use strict";
// The public page's renderer (tools/board.js) on fake readings: node --test tests/v2/test_board_js.js

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../tools/board.js"), "utf8");
const T = 1795663200, H = 3600, TOTAL = 275456;

function render(data) {
  const elements = new Map();
  const document = {
    querySelector: () => ({ content: "./snapshot.json" }),
    getElementById: id => {
      if (!elements.has(id)) elements.set(id, {});
      return elements.get(id);
    },
    addEventListener: () => {},
  };
  const context = { document, data, fetch: () => new Promise(() => {}), setInterval: () => {}, AbortSignal };
  vm.runInNewContext(source + "\nrender(data);", context);
  const html = elements.get("tiles").innerHTML;
  const tiles = {};
  for (const m of html.matchAll(/<article class="tile[^"]*" aria-label="GPU (\d+),[\s\S]*?<\/article>/g)) tiles[m[1]] = m[0];
  return { tiles, summary: elements.get("summary").innerHTML, calendar: elements.get("calendar").innerHTML };
}

const gpu = (id, mem_mb, util, users) => ({ gpu: id, mem_mb, util, total_mb: TOTAL, users, available: true });
const reading = () => ({
  t: T,
  bookings: [{ user: "alice", gpus: [0], start: T - H, end: T + 3 * H, state: "standing", objection_close: null }],
  entries: [{ user: "alice", gpus: [0], started: T - H, until: T + 3 * H, kind: "scheduled" },
            { user: "bob", gpus: [1], started: T - H, until: T + 8 * H, kind: "backfill" }],
  gpus: [gpu(0, 120000, 98, { alice: 119900 }), gpu(1, 80000, 90, { bob: 79900 }),
         gpu(2, 140000, 1, { carol: 139800 }), gpu(3, 0, 0, {})],
});

test("telemetry keeps unattributed GPUs out of the free count", () => {
  const data = reading();
  data.entries = [{ user: "alice", gpus: [1, 2], kind: "backfill" }];
  data.gpus = [[154624, 0], [0, 1], [0, 0], [2048, null]].map(([mem_mb, util], id) => gpu(id, mem_mb, util, {}));
  const { tiles, summary } = render(data);
  assert.match(tiles[0], /aria-label="GPU 0, In use, owner unknown"/);
  assert.match(tiles[1], /aria-label="GPU 1, In use, owner unknown"/);
  assert.match(tiles[2], /aria-label="GPU 2, Claimed"/);
  assert.match(tiles[3], /aria-label="GPU 3, Free"/);
  assert.match(summary, /<b>1<\/b> of 4 free/);
});

test("a scheduled tile names its owner and window", () => {
  const t = render(reading()).tiles[0];
  assert.match(t, /class="tile busy has-scheduled"/);
  assert.match(t, /<li class="who scheduled">/);
  assert.match(t, /class="task scheduled"><div class="task-meta"><span class="dot"[^>]*><\/span><b>alice<\/b>/);
  assert.match(t, /<span class="until">\d\d:\d\d–[^<]*\d\d:\d\d<\/span>/);
});

test("a backfill tile is muted and does not repeat the name", () => {
  const t = render(reading()).tiles[1];
  assert.match(t, /class="tile busy backfill-only"/);
  assert.match(t, /<li class="who backfill">/);
  assert.match(t, /class="seg backfill"/);
  const task = t.slice(t.indexOf('class="task backfill"'));
  assert.doesNotMatch(task, /bob/);
  assert.match(task, /<span class="tag backfill">Backfill<\/span>/);
  assert.match(task, /until /);
});

test("GPU use with no board entry is a warning", () => {
  const { tiles, summary, calendar } = render(reading());
  assert.match(tiles[2], /class="tile offboard"/);
  assert.match(tiles[2], /<span class="state-pill offboard">Not on board<\/span>/);
  assert.match(tiles[2], /<li class="who off">/);
  assert.match(tiles[2], /<span class="flag">Not on board<\/span>/);
  assert.match(summary, /class="chip alert offboard"><b>1<\/b> GPU not on board/);
  assert.match(summary, /<b>1<\/b> of 4 free/);
  assert.match(calendar, /class="blk unreg /);
});

test("a GPU in use with no owner is never Free", () => {
  const data = reading();
  data.gpus[1] = gpu(1, 150000, 100, { "Unknown user": 149900 });
  data.entries = data.entries.filter(entry => entry.user !== "bob");
  data.gpus[3] = gpu(3, 154624, 1, {});  // memory and utilization, no process listed
  const { tiles, summary, calendar } = render(data);
  for (const id of [1, 3]) {
    assert.match(tiles[id], new RegExp(`aria-label="GPU ${id}, In use, owner unknown"`));
    assert.match(tiles[id], /class="tile unattributed/);
    assert.match(tiles[id], /<span class="flag">Owner unknown<\/span>/);
    assert.doesNotMatch(tiles[id], /Free/);
  }
  assert.match(tiles[3], /class="seg unknown"/);
  assert.match(summary, /class="chip alert unattributed"><b>2<\/b> GPUs in use, owner unknown/);
  assert.match(summary, /<b>0<\/b> of 4 free/);
  assert.match(calendar, /class="blk unreg unknown/);
});
