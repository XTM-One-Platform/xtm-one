/**
 * randomId(): crypto.randomUUID when present, a getRandomValues v4 UUID on a
 * non-secure origin where randomUUID is undefined.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, relative, sep } from "node:path";

import { randomId } from "./random-id.ts";

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const withoutRandomUUID = {
  getRandomValues: (array: Uint8Array<ArrayBuffer>) => globalThis.crypto.getRandomValues(array),
};

test("uses crypto.randomUUID when the context offers it", () => {
  const source = { ...withoutRandomUUID, randomUUID: () => "11111111-2222-4333-8444-555555555555" };
  assert.equal(randomId(source), "11111111-2222-4333-8444-555555555555");
});

test("builds a v4 UUID from getRandomValues when randomUUID is undefined", () => {
  const ids = new Set<string>();
  for (let i = 0; i < 200; i += 1) {
    const id = randomId(withoutRandomUUID);
    assert.match(id, V4);
    ids.add(id);
  }
  assert.equal(ids.size, 200);
});

test("sets the version and variant bits whatever the random bytes", () => {
  const ones = { getRandomValues: (array: Uint8Array) => array.fill(0xff) };
  const zeros = { getRandomValues: (array: Uint8Array) => array };
  assert.equal(randomId(ones), "ffffffff-ffff-4fff-bfff-ffffffffffff");
  assert.equal(randomId(zeros), "00000000-0000-4000-8000-000000000000");
});

test("the global crypto gives a v4 UUID", () => {
  assert.match(randomId(), V4);
});

// A direct call throws on a non-secure origin: only random-id.ts may
// call it, tests excepted.
test("no source file calls crypto.randomUUID( outside lib/random-id.ts", () => {
  const src = fileURLToPath(new URL("..", import.meta.url));
  const offenders = (readdirSync(src, { recursive: true }) as string[])
    .filter((path) => /\.(ts|tsx)$/.test(path) && !/\.test\.tsx?$/.test(path))
    .filter((path) => !path.split(sep).includes("node_modules"))
    .filter((path) => relative("lib", path) !== "random-id.ts")
    .filter((path) => /\bcrypto\.randomUUID\s*\(/.test(readFileSync(join(src, path), "utf8")));
  assert.deepEqual(offenders, [], "use randomId() from lib/random-id.ts instead");
});
