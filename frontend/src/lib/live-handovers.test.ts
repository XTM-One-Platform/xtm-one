/**
 * Live handover edges of the agent graph - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  LIVE_HANDOVER_WINDOW_MS,
  isLiveHandover,
  liveEdgesKey,
  liveHandoverEdges,
  type LiveHandoverSource,
} from "./live-handovers.ts";

const NOW = Date.parse("2026-11-05T20:00:00Z");
const MIN = 60 * 1000;

function iso(msAgo: number): string {
  return new Date(NOW - msAgo).toISOString();
}

function handover(over: Partial<LiveHandoverSource>): LiveHandoverSource {
  return {
    parent_agent_id: "a",
    target_agent_id: "b",
    status: "completed",
    created_at: iso(2 * MIN),
    completed_at: iso(1 * MIN),
    ...over,
  };
}

test("a running or pending handover is live whatever its age", () => {
  assert.equal(isLiveHandover(handover({ status: "running", created_at: iso(600 * MIN), completed_at: null }), NOW), true);
  assert.equal(isLiveHandover(handover({ status: "pending", created_at: iso(600 * MIN), completed_at: null }), NOW), true);
});

test("an ended handover stays live for the window after it ended, then goes still", () => {
  assert.equal(isLiveHandover(handover({ completed_at: iso(LIVE_HANDOVER_WINDOW_MS) }), NOW), true);
  assert.equal(isLiveHandover(handover({ completed_at: iso(LIVE_HANDOVER_WINDOW_MS + 1) }), NOW), false);
  assert.equal(isLiveHandover(handover({ status: "failed", completed_at: iso(5 * MIN) }), NOW), true);
});

test("an ended handover with no end time is judged on when it fired", () => {
  assert.equal(isLiveHandover(handover({ created_at: iso(5 * MIN), completed_at: null }), NOW), true);
  assert.equal(isLiveHandover(handover({ created_at: iso(60 * MIN), completed_at: null }), NOW), false);
  assert.equal(isLiveHandover(handover({ created_at: "not a date", completed_at: null }), NOW), false);
});

test("an old handover among the newest rows no longer makes its edge live", () => {
  // The defect: the map kept every edge of the last 40 delegations live.
  const edges = liveHandoverEdges(
    [
      handover({ parent_agent_id: "ctem", target_agent_id: "opencti", created_at: iso(300 * MIN), completed_at: iso(299 * MIN) }),
      handover({ parent_agent_id: "x", target_agent_id: "y", status: "running", created_at: iso(1 * MIN), completed_at: null }),
    ],
    NOW,
  );
  assert.deepEqual([...edges.keys()], ["x->y"]);
});

test("an edge carries its newest live handover and the node mapping", () => {
  const edges = liveHandoverEdges(
    [
      handover({ created_at: iso(10 * MIN), completed_at: iso(9 * MIN) }),
      handover({ status: "running", created_at: iso(1 * MIN), completed_at: null }),
    ],
    NOW,
    (id) => (id === "b" ? "fleet:f1" : id),
  );
  assert.deepEqual(edges.get("a->fleet:f1"), { status: "running", firedAt: iso(1 * MIN) });
  assert.equal(edges.size, 1);
});

test("no delegations, no live edge", () => {
  assert.equal(liveHandoverEdges(undefined, NOW).size, 0);
  assert.equal(liveHandoverEdges([], NOW).size, 0);
});

test("liveEdgesKey: a clock tick that changes no live edge keeps the key", () => {
  const delegations = [
    handover({ status: "running", created_at: iso(2 * MIN) }),
    handover({ parent_agent_id: "c", target_agent_id: "d", status: "completed", created_at: iso(5 * MIN), completed_at: iso(4 * MIN) }),
  ];
  const before = liveHandoverEdges(delegations, NOW);
  const after = liveHandoverEdges(delegations, NOW + 30 * 1000);
  assert.notEqual(before, after);
  assert.equal(liveEdgesKey(after), liveEdgesKey(before));
  // A status change or an edge leaving the window changes it.
  const ended = liveHandoverEdges([{ ...delegations[0], status: "completed", completed_at: iso(0) }, delegations[1]], NOW);
  assert.notEqual(liveEdgesKey(ended), liveEdgesKey(before));
  assert.notEqual(liveEdgesKey(liveHandoverEdges(delegations, NOW + LIVE_HANDOVER_WINDOW_MS)), liveEdgesKey(before));
  assert.equal(liveEdgesKey(undefined), "");
});
