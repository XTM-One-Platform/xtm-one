/**
 * The run splitting and the weighing behind `yarn e2e:shard-timings` -
 * `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { collectTimings, emptyRunIndexes, median, splitRuns } from "./e2e-shard-timings.mjs";

test("reports of one run stay in one group, `--` opens the next", () => {
  assert.deepEqual(splitRuns(["a.json", "b.json"]), [["a.json", "b.json"]]);
  assert.deepEqual(splitRuns(["a.json", "--", "b.json", "c.json", "--", "d.json"]), [
    ["a.json"],
    ["b.json", "c.json"],
    ["d.json"],
  ]);
});

test("a leading, trailing or doubled `--` leaves a run with no report", () => {
  assert.deepEqual(emptyRunIndexes(splitRuns([])), [0]);
  assert.deepEqual(emptyRunIndexes(splitRuns(["a.json", "--"])), [1]);
  assert.deepEqual(emptyRunIndexes(splitRuns(["--", "a.json"])), [0]);
  assert.deepEqual(emptyRunIndexes(splitRuns(["a.json", "--", "--", "b.json"])), [1]);
  assert.deepEqual(emptyRunIndexes(splitRuns(["a.json", "b.json"])), []);
});

test("the median is the middle value, or the average of the two middles", () => {
  assert.equal(median([5]), 5);
  assert.equal(median([9, 1, 5]), 5);
  assert.equal(median([10, 1, 4, 7]), 5.5);
  assert.equal(median([3, 3]), 3);
});

test("a single run weighs exactly what it measured", () => {
  assert.equal(median([29.9]), 29.9);
});

test("a report sums the first attempt of every test of a spec, and recurses into suites", () => {
  const measured = {};
  collectTimings(
    {
      specs: [{ file: "chat-turn.spec.ts", tests: [{ results: [{ duration: 1000 }, { duration: 9000 }] }] }],
      suites: [
        {
          specs: [{ file: "a2a/01-oauth-config.spec.ts", tests: [{ results: [{ duration: 2000 }] }] }],
        },
      ],
    },
    measured,
  );
  assert.deepEqual(measured, { "chat-turn.spec.ts": 1, "a2a/01-oauth-config.spec.ts": 2 });
});

test("a test with no result or a non-numeric duration weighs nothing", () => {
  const measured = {};
  collectTimings(
    {
      specs: [
        { file: "skipped.spec.ts", tests: [{ results: [] }] },
        { file: "not-timed.spec.ts", tests: [{ results: [{ duration: undefined }] }] },
        { file: "no-tests.spec.ts" },
      ],
    },
    measured,
  );
  assert.deepEqual(measured, {});
});
