/**
 * Unit tests for the relays switch reader - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

import { relaysEnabledFrom } from "./relays.ts";

test("relays are not known while /auth/config loads, so the page never says they are off", () => {
  assert.equal(relaysEnabledFrom(undefined, true), undefined);
  assert.equal(relaysEnabledFrom({ relays_enabled: false }, true), undefined);
});

test("relays follow RELAYS_ENABLED once /auth/config answered", () => {
  assert.equal(relaysEnabledFrom({ relays_enabled: true }, false), true);
  assert.equal(relaysEnabledFrom({ relays_enabled: false }, false), false);
});

test("a config without the flag, or that failed to load, keeps the server default: on", () => {
  assert.equal(relaysEnabledFrom({}, false), true);
  assert.equal(relaysEnabledFrom(undefined, false), true);
});

// A surface reading the flag itself decides alone what a loading config
// means, and `relays_enabled === true` reads it as off.
test("only relaysEnabledFrom reads relays_enabled", () => {
  const SRC = new URL("../", import.meta.url);
  const readers = readdirSync(SRC, { recursive: true })
    .map((path) => String(path).replace(/\\/g, "/"))
    .filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path))
    .filter((path) => /\.relays_enabled\b/.test(readFileSync(new URL(path, SRC), "utf8")))
    .sort();
  assert.deepEqual(readers, ["lib/relays.ts"]);
});
