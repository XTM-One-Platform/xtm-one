/**
 * Unit tests for the agent Overview's A2A binding badge - `yarn test:unit`.
 *
 * The badge must say what binding the registration does - expose the agent or
 * give it a delegation tool - and say nothing it cannot currently stand by.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { a2aBindingLabel } from "./a2a-binding-label.ts";

test("an inbound binding exposes the agent", () => {
  assert.equal(a2aBindingLabel(true, "inbound"), "Exposes agent");
});

test("an outbound binding delegates to the partner, never exposes", () => {
  assert.equal(a2aBindingLabel(true, "outbound"), "Delegates to partner");
});

test("an unknown direction shows no badge rather than guessing one", () => {
  // Still loading, or a lookup that failed: neither answers the question.
  assert.equal(a2aBindingLabel(true, undefined), null);
});

test("with A2A not offered, a direction cached from before shows nothing", () => {
  assert.equal(a2aBindingLabel(false, "outbound"), null);
  assert.equal(a2aBindingLabel(false, "inbound"), null);
});

test("the row's direction lookup can neither take the agent page down nor poll", () => {
  // Read off the source: the row needs a browser and the `@/` alias, and
  // `yarn test:unit` has neither. The A2A e2e suite fails the lookup for real.
  // A bound registration cannot change direction, so a poll would only repeat
  // the read, and with it the partner discovery a manager's read runs.
  const row = readFileSync(new URL("./OverviewTab.tsx", import.meta.url), "utf8");
  assert.match(
    row,
    /useA2ADirection\(\s*isA2A \? integration : undefined,\s*\{\s*throwOnError:\s*false,\s*refetchInterval:\s*false\s*\}\s*\)/,
  );
});
