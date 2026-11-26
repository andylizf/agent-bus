"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("telemetry keeps unattributed GPUs out of the free count", () => {
  const elements = new Map();
  const document = {
    querySelector: () => ({ content: "./snapshot.json" }),
    getElementById: id => {
      if (!elements.has(id)) elements.set(id, {});
      return elements.get(id);
    },
    addEventListener: () => {},
  };
  const source = fs.readFileSync(path.join(__dirname, "../../tools/board.js"), "utf8");
  const data = {
    t: 1795663200,
    bookings: [],
    entries: [{ user: "alice", gpus: [1, 2], kind: "backfill" }],
    gpus: [[154624, 0], [0, 1], [0, 0], [2048, null]].map(([mem_mb, util], gpu) =>
      ({ gpu, mem_mb, util, total_mb: 275456, users: {}, available: true })),
  };
  const context = { document, data, fetch: () => new Promise(() => {}), setInterval: () => {}, AbortSignal };
  vm.runInNewContext(source + "\nrender(data);", context);
  const tiles = elements.get("tiles").innerHTML;
  assert.match(tiles, /aria-label="GPU 0, In use, owner unknown"/);
  assert.match(tiles, /aria-label="GPU 1, In use, owner unknown"/);
  assert.equal((tiles.match(/class="state-pill unattributed flag"/g) || []).length, 2);
  assert.match(tiles, /aria-label="GPU 2, Claimed"/);
  assert.match(tiles, /aria-label="GPU 3, Free"/);
  assert.match(elements.get("summary").innerHTML, /<b>1<\/b> of 4 free/);
});
