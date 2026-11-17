/**
 * Unit tests for the pack builder's draft edits - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  MAX_ASSIST_HISTORY,
  assistHistory,
  addSource,
  addStep,
  emptyDraft,
  moveStep,
  newSource,
  pivotTargets,
  refusalText,
  removeSource,
  removeStep,
  setExposed,
  stepKey,
  MAX_STEPS,
} from "./pack-draft.ts";

function threeSteps() {
  let draft = emptyDraft();
  draft = addStep(draft, "Map the hosting");
  draft = addStep(draft, "Who else is on that IP");
  draft = addSource(draft, 1, { ...newSource("passive_dns"), pivot_step: "who-else-is-on-that-ip" });
  draft = setExposed(draft, { step: "map-the-hosting", source: 0, setting: "max_pages", default: "5" }, true);
  return draft;
}

test("a step key is a unique slug of its label", () => {
  assert.equal(stepKey("Map the hosting", []), "map-the-hosting");
  assert.equal(stepKey("Qualifier l'été", []), "qualifier-l-ete");
  assert.equal(stepKey("Map the hosting", ["map-the-hosting"]), "map-the-hosting-2");
  assert.equal(stepKey("!!!", []), "step");
});

test("removing a step drops the pivots into it and the settings it exposed", () => {
  const draft = removeStep(threeSteps(), 2);
  assert.deepEqual(draft.definition.steps.map((s) => s.key), ["qualify", "map-the-hosting"]);
  assert.equal(draft.definition.steps[1].sources[0].pivot_step, null);
  const without = removeStep(threeSteps(), 1);
  assert.deepEqual(without.definition.exposed, []);
});

test("moving a step never leaves a pivot pointing backwards", () => {
  const moved = moveStep(threeSteps(), 2, -1);
  assert.deepEqual(moved.definition.steps.map((s) => s.key), ["qualify", "who-else-is-on-that-ip", "map-the-hosting"]);
  assert.equal(moved.definition.steps[2].sources[0].pivot_step, null);
  assert.deepEqual(moveStep(threeSteps(), 0, -1).definition.steps.map((s) => s.key), ["qualify", "map-the-hosting", "who-else-is-on-that-ip"]);
});

test("removing a source re-points the settings exposed on the sources after it", () => {
  let draft = threeSteps();
  draft = addSource(draft, 1, newSource("site_fingerprint"));
  draft = addSource(draft, 1, newSource("passive_dns"));
  draft = setExposed(draft, { step: "map-the-hosting", source: 2, setting: "hostname_keywords", default: "none" }, true);
  draft = removeSource(draft, 1, 0);
  assert.deepEqual(draft.definition.exposed, [
    { step: "map-the-hosting", source: 1, setting: "hostname_keywords", default: "none" },
  ]);
});

test("a source pivots into its own step or a later one", () => {
  assert.deepEqual(pivotTargets(threeSteps(), 1).map((s) => s.key), ["map-the-hosting", "who-else-is-on-that-ip"]);
});

test("the step count is capped", () => {
  let draft = emptyDraft();
  for (let i = 0; i < MAX_STEPS + 3; i++) draft = addStep(draft, `Step ${i}`);
  assert.equal(draft.definition.steps.length, MAX_STEPS);
});

test("a new pack starts with a lookup that answers its subjects, so it can be saved as it is", () => {
  const catalog = [
    { name: "NVD", subject_types: ["cve"] },
    { name: "RDAP registration (IP)", subject_types: ["ip"] },
    { name: "RDAP registration (domain)", subject_types: ["domain", "url"] },
  ];
  assert.deepEqual(emptyDraft(catalog).definition.steps[0].sources, [
    { kind: "reference", reference: "RDAP registration (domain)" },
  ]);
  assert.deepEqual(emptyDraft().definition.steps[0].sources, []);
});

test("a refused save says what the server said, not its status", () => {
  const refused = Object.assign(new Error("HTTP 422"), {
    detail: [
      { loc: ["body", "definition"], msg: "Value error, step 'b' has no source and no step pivots into it" },
      { loc: ["body", "name"], msg: "String should have at least 1 character" },
    ],
  });
  assert.equal(
    refusalText(refused),
    "step 'b' has no source and no step pivots into it · String should have at least 1 character",
  );
  assert.equal(refusalText(new Error("Investigation pack not found")), "Investigation pack not found");
  assert.equal(refusalText(null), null);
});

test("the assistant's history leaves the panel's own error bubbles out", () => {
  assert.deepEqual(
    assistHistory([
      { role: "user", content: "map the hosting" },
      { role: "assistant", content: "The assistant could not build a pack.", failed: true },
      { role: "user", content: "map the hosting, again" },
      { role: "assistant", content: "Two steps." },
    ]),
    [
      { role: "user", content: "map the hosting" },
      { role: "user", content: "map the hosting, again" },
      { role: "assistant", content: "Two steps." },
    ],
  );
});

test("the assistant's history sends its newest turns, within what a round accepts", () => {
  const turns = Array.from({ length: 30 }, (_, i) => ({
    role: (i % 2 ? "assistant" : "user") as "user" | "assistant",
    content: `turn ${i}`,
  }));
  const sent = assistHistory([...turns, { role: "assistant", content: "failed", failed: true }]);
  assert.equal(MAX_ASSIST_HISTORY, 20);
  assert.deepEqual(
    sent.map((t) => t.content),
    turns.slice(10).map((t) => t.content),
  );
});
