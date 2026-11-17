/**
 * Unit tests for the run feed agent filter - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  MAX_RUN_FILTER_AGENT_IDS,
  exceedsRunAgentFilter,
  runAgentIdsParam,
} from "./run-agent-filter.ts";

test("the agent ids are sent sorted and deduplicated", () => {
  assert.equal(runAgentIdsParam(new Set(["b", "a", "c"])), "a,b,c");
  assert.equal(runAgentIdsParam(["b", "a", "b"]), "a,b");
});

test("no agent is an empty filter, not an absent one", () => {
  assert.equal(runAgentIdsParam(new Set()), "");
  assert.equal(runAgentIdsParam(undefined), undefined);
});

test("more agents than the server filters by is detected, never cut", () => {
  const ids = Array.from({ length: MAX_RUN_FILTER_AGENT_IDS }, (_, i) => `agent-${i}`);
  assert.equal(exceedsRunAgentFilter(ids), false);
  assert.equal(exceedsRunAgentFilter(new Set([...ids, "one-more"])), true);
  assert.equal(exceedsRunAgentFilter([...ids, ids[0]]), false);
});
