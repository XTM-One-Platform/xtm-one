/**
 * The E2E jobs of one run share out the spec files (tests_e2e/shards.ts): a
 * file dealt to no job is a spec CI silently stopped running, a file dealt to
 * two is run twice. Run with `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { E2E_DIR, deal, parseShard, readTimings, specFiles } from "../tests_e2e/shards.ts";

const PROJECTS = [
  { name: "core", dir: E2E_DIR, skip: ["a2a", "opencti"] },
  { name: "a2a", dir: path.join(E2E_DIR, "a2a"), skip: [] },
] as const;

test("every spec file of a project runs in exactly one job", () => {
  for (const project of PROJECTS) {
    const files = specFiles(project.dir, project.skip);
    assert.ok(files.length > 0, `${project.name} has no spec file`);
    for (const total of [1, 2, 3, 5, 8]) {
      const jobs = deal(files, total, readTimings());
      assert.equal(jobs.length, total);
      assert.deepEqual(jobs.flat().sort(), files, `${project.name} in ${total} jobs`);
    }
  }
});

test("core never deals an A2A spec, and A2A only its own", () => {
  const core = specFiles(E2E_DIR, ["a2a", "opencti"]);
  const a2a = specFiles(path.join(E2E_DIR, "a2a"));
  assert.ok(core.every((file) => !file.startsWith("a2a/")));
  assert.ok(a2a.length > 0 && a2a.every((file) => file.startsWith("a2a/")));
});

test("the deal does not depend on the order the files are listed in", () => {
  const files = specFiles(E2E_DIR, ["a2a", "opencti"]);
  const timings = readTimings();
  assert.deepEqual(deal([...files].reverse(), 5, timings), deal(files, 5, timings));
});

test("the heaviest files go to different jobs", () => {
  const timings = { "a.spec.ts": 100, "b.spec.ts": 90, "c.spec.ts": 10, "d.spec.ts": 10, "e.spec.ts": 10 };
  const jobs = deal(Object.keys(timings), 2, timings);
  assert.ok(!jobs.some((job) => job.includes("a.spec.ts") && job.includes("b.spec.ts")));
  assert.deepEqual(jobs, [["a.spec.ts", "d.spec.ts"], ["b.spec.ts", "c.spec.ts", "e.spec.ts"]]);
});

test("a file with no timing yet weighs the median of the timed ones", () => {
  const timings = { "a.spec.ts": 30, "b.spec.ts": 10, "c.spec.ts": 20 };
  // Weighing 20, x.spec.ts follows c.spec.ts to the second job; weighing
  // little, it would have joined a.spec.ts on the first.
  assert.deepEqual(deal(["a.spec.ts", "b.spec.ts", "c.spec.ts", "x.spec.ts"], 2, timings), [
    ["a.spec.ts", "b.spec.ts"],
    ["c.spec.ts", "x.spec.ts"],
  ]);
});

test("E2E_SHARD reads <index>/<total> and nothing else", () => {
  assert.equal(parseShard(undefined), undefined);
  assert.equal(parseShard(""), undefined);
  assert.deepEqual(parseShard("2/5"), { index: 2, total: 5 });
  for (const bad of ["0/3", "4/3", "1/0", "2", "a/b", "1/3/5"]) {
    assert.throws(() => parseShard(bad), /E2E_SHARD/, bad);
  }
});

test("a table a run's select job handed out is read, and one that does not read stops the job", () => {
  const dir = mkdtempSync(path.join(tmpdir(), "e2e-shards-"));
  const table = path.join(dir, "e2e.json");
  writeFileSync(table, JSON.stringify({ "agents.spec.ts": 42 }));
  assert.deepEqual(readTimings(table, true), { "agents.spec.ts": 42 });
  // Falling back alone would deal the files unlike the other jobs: a spec would run twice or never.
  assert.throws(() => readTimings(path.join(dir, "missing.json"), true), /E2E_SHARD_TIMINGS/);
  assert.deepEqual(readTimings(path.join(dir, "missing.json"), false), {});
});
