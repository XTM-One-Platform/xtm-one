/**
 * The in-progress segment of the chat composer's agentic counter -
 * `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  addTurnCharge,
  agenticUsagePeriod,
  chargeFromFrame,
  NO_TURN_CHARGES,
  refetchAfterCharge,
  splitAgenticCount,
} from "./agentic-usage.ts";

const at = (ms: number, count = 1, period?: string) => ({ at: ms, count, ...(period ? { period } : {}) });
const relayedAt = (ms: number, count = 1) => ({ at: ms, count, relayed: true as const });

test("a frame charges the actions it announces", () => {
  assert.deepEqual(chargeFromFrame(1, 500), at(500));
  assert.deepEqual(chargeFromFrame(2, 500, "2026-09-27"), at(500, 2, "2026-09-27"));
});

test("a frame announcing no action, or no readable count, charges nothing", () => {
  // The same counts the backend journal refuses (`_charged_actions`): a
  // malformed frame never shows a phantom action.
  for (const actions of [0, -3, undefined, null, "2", 1.5, true, false, Number.NaN]) {
    assert.equal(chargeFromFrame(actions, 500), null, String(actions));
  }
});

test("a replayed total keeps every action, however many passes the turn ran", () => {
  const charge = chargeFromFrame(250, 500);
  assert.ok(charge);
  assert.deepEqual(splitAgenticCount(1_000, [charge], 400), { before: 1_000, inTurn: 250, total: 1_250 });
});

test("no charge: the count is what came before", () => {
  assert.deepEqual(splitAgenticCount(4, NO_TURN_CHARGES, 1_000), { before: 4, inTurn: 0, total: 4 });
});

test("a charge the last fetch predates is added to its count", () => {
  // Fetched at the end of the previous turn, charged when this one started.
  assert.deepEqual(splitAgenticCount(4, [at(2_000)], 1_000), { before: 4, inTurn: 1, total: 5 });
});

test("a charge the last fetch already counts is taken out of it", () => {
  // The refetch the frame triggered landed after it.
  assert.deepEqual(splitAgenticCount(5, [at(2_000)], 2_100), { before: 4, inTurn: 1, total: 5 });
});

test("the passes of one turn add up, each against the fetch that saw it", () => {
  // Pass 1 charged and refetched, pass 2 charged and not refetched yet.
  assert.deepEqual(splitAgenticCount(5, [at(2_000), at(3_000)], 2_100), { before: 4, inTurn: 2, total: 6 });
  // Both refetched.
  assert.deepEqual(splitAgenticCount(6, [at(2_000), at(3_000)], 3_100), { before: 4, inTurn: 2, total: 6 });
});

test("a charge taken in another quota period is left to that period's total", () => {
  // A daily turn charged before midnight, charged again after; the last fetch
  // describes the new day and holds only the second charge.
  const charges = [at(2_000, 1, "2026-09-26"), at(3_000, 1, "2026-09-27")];
  assert.deepEqual(splitAgenticCount(1, charges, 3_100, "2026-09-27"), { before: 0, inTurn: 1, total: 1 });
  // A charge or a fetch with no period is not second-guessed.
  assert.deepEqual(splitAgenticCount(1, [at(2_000)], 2_100, "2026-09-27"), { before: 0, inTurn: 1, total: 1 });
});

test("the split never reads below zero", () => {
  assert.deepEqual(splitAgenticCount(0, [at(2_000)], 2_100), { before: 0, inTurn: 1, total: 1 });
});

test("the period is read off the agentic total, not a per-model or sandbox entry", () => {
  const usage = [
    { usage_type: "llm_proxy", model: null, period: "2026-09" },
    { usage_type: "agentic", model: "gpt", period: "2026-08" },
    { usage_type: "agentic", model: null, period: "2026-09-27" },
    { usage_type: "sandbox", model: null, period: "2026-09-27" },
  ];
  assert.equal(agenticUsagePeriod(usage), "2026-09-27");
  assert.equal(agenticUsagePeriod(undefined), undefined);
  assert.equal(agenticUsagePeriod([]), undefined);
});

test("a relayed frame marks its charge", () => {
  assert.deepEqual(chargeFromFrame(1, 500, "2026-09-27", true), { ...at(500, 1, "2026-09-27"), relayed: true });
  assert.deepEqual(chargeFromFrame(1, 500, undefined, false), at(500));
});

test("the relayed turn's charges add up, and a charge of this page's own turn replaces them", () => {
  // Following a turn another tab runs, a send folded into it included.
  assert.deepEqual(addTurnCharge([relayedAt(1_000)], relayedAt(2_000, 2)), [relayedAt(1_000), relayedAt(2_000, 2)]);
  // That turn ended unseen and this page runs the next one: the relayed
  // charges are in the period total, not in the reply in progress.
  assert.deepEqual(addTurnCharge([relayedAt(1_000), relayedAt(2_000, 2)], at(3_000)), [at(3_000)]);
  assert.deepEqual(addTurnCharge([at(3_000)], at(4_000)), [at(3_000), at(4_000)]);
  assert.deepEqual(addTurnCharge(NO_TURN_CHARGES, at(3_000)), [at(3_000)]);
});

test("a quota request sent before the charge is replaced, never joined", { timeout: 5_000 }, async () => {
  const { QueryClient, QueryObserver } = await import("@tanstack/query-core");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // The counter the server holds: 4 before the charge, 5 after.
  let count = 4;
  const requests: Array<{ answer: (value: number) => void; sentAt: number }> = [];
  let now = 1_000;
  const observer = new QueryObserver(client, {
    queryKey: ["my-quotas"],
    queryFn: ({ signal }) =>
      new Promise<number>((resolve, reject) => {
        const sampled = count;
        requests.push({ answer: () => resolve(sampled), sentAt: now });
        signal.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  });
  const unsubscribe = observer.subscribe(() => {});
  try {
    // The page's first fetch left before the charge (no data yet).
    assert.equal(requests.length, 1);
    now = 2_000;
    count = 5;
    const charge = chargeFromFrame(1, now);
    assert.ok(charge);
    const refetched = refetchAfterCharge(client, ["my-quotas"]);
    await new Promise((resolve) => setImmediate(resolve));
    // The request that read 4 is not the one the counter will read.
    assert.equal(requests.length, 2);
    assert.equal(requests[1].sentAt, 2_000);
    requests[0].answer(0);
    requests[1].answer(0);
    await refetched;
    const state = client.getQueryState<number>(["my-quotas"]);
    assert.equal(state?.data, 5);
    now = 2_100;
    assert.deepEqual(splitAgenticCount(state?.data ?? 0, [charge], now), { before: 4, inTurn: 1, total: 5 });
  } finally {
    unsubscribe();
    client.clear();
  }
});

