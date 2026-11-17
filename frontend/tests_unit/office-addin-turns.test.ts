/**
 * The Office add-in's turn watch (`apps/microsoft-office/src/lib/turns.ts`):
 * a review of the document asks one question per section, each its own turn.
 * The next section goes only once the previous turn has its answer, never
 * while it runs (it would steer that turn), and the review ends at a Stop, a
 * failed send or a closed pane.
 *
 * Run with `yarn test:unit` (Node's built-in runner).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  createTurnWatch,
  runOnePerTurn,
  type SequenceStep,
  type TurnWatch,
} from "../../apps/microsoft-office/src/lib/turns.ts";

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

const steps: SequenceStep[] = [1, 2, 3].map((n) => ({ text: `section ${n}`, displayText: `shown ${n}` }));

/** A panel stand-in: each send starts a turn; the test ends it. */
function fakePanel(watch: TurnWatch, outcomes: Record<number, string> = {}) {
  const sent: string[] = [];
  let running = false;
  let sentDuringTurn = false;
  const send = async (step: SequenceStep) => {
    const outcome = outcomes[sent.length] ?? "sent";
    if (outcome === "sent") {
      sentDuringTurn ||= running;
      sent.push(step.text);
      running = true;
      watch.setRunning(true);
    }
    return outcome;
  };
  const end = (how: "answered" | "stopped" | "failed") => {
    running = false;
    watch.setRunning(false, how);
  };
  return { sent, send, end, sentDuringTurn: () => sentDuringTurn };
}

test("each section is sent once the previous turn has its answer, never during it", async () => {
  const watch = createTurnWatch();
  const panel = fakePanel(watch);
  const run = runOnePerTurn(steps, panel.send, watch);
  await tick();
  assert.deepEqual(panel.sent, ["section 1"]);
  panel.end("answered");
  await tick();
  assert.deepEqual(panel.sent, ["section 1", "section 2"]);
  panel.end("answered");
  await tick();
  assert.deepEqual(panel.sent, ["section 1", "section 2", "section 3"]);
  assert.equal(await run, 3);
  assert.equal(panel.sentDuringTurn(), false);
});

test("a turn the panel says was stopped or failed ends the review", async () => {
  for (const how of ["stopped", "failed"] as const) {
    const watch = createTurnWatch();
    const panel = fakePanel(watch);
    const run = runOnePerTurn(steps, panel.send, watch);
    await tick();
    panel.end(how);
    assert.equal(await run, 1, how);
    assert.deepEqual(panel.sent, ["section 1"], how);
  }
});

test("a turn end without an outcome counts as stopped", async () => {
  const watch = createTurnWatch();
  const panel = fakePanel(watch);
  const run = runOnePerTurn(steps, panel.send, watch);
  await tick();
  watch.setRunning(false);
  assert.equal(await run, 1);
});

test("a send that does not start a turn of its own ends the review", async () => {
  for (const outcome of ["failed", "dropped", "steered", "held"]) {
    const watch = createTurnWatch();
    const panel = fakePanel(watch, { 1: outcome });
    const run = runOnePerTurn(steps, panel.send, watch);
    await tick();
    panel.end("answered");
    assert.equal(await run, 1, outcome);
  }
});

test("the first section waits for a turn already running", async () => {
  const watch = createTurnWatch();
  watch.setRunning(true);
  const panel = fakePanel(watch);
  const run = runOnePerTurn(steps.slice(0, 1), panel.send, watch);
  await tick();
  assert.deepEqual(panel.sent, []);
  watch.setRunning(false, "answered");
  assert.equal(await run, 1);
  assert.deepEqual(panel.sent, ["section 1"]);
});

test("a closed watch (the pane is gone) or a cancelled review ends it", async () => {
  const watch = createTurnWatch();
  const panel = fakePanel(watch);
  const run = runOnePerTurn(steps, panel.send, watch);
  await tick();
  watch.close();
  assert.equal(await run, 1);
  assert.equal(await watch.idle(), false);

  const other = createTurnWatch();
  const second = fakePanel(other);
  let cancelled = false;
  const review = runOnePerTurn(steps, second.send, other, () => cancelled);
  await tick();
  cancelled = true;
  second.end("answered");
  assert.equal(await review, 1);
  assert.deepEqual(second.sent, ["section 1"]);
});

test("a turn that ended before the wait began is still the one waited for", async () => {
  const watch = createTurnWatch();
  const since = watch.starts();
  watch.setRunning(true);
  watch.setRunning(false, "answered");
  assert.equal(await watch.endAfter(since), "answered");
});
