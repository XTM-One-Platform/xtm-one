/**
 * Unit tests for the flow card status display - `yarn test:unit`.
 *
 * Pinned: every known status maps to its own label (an existing i18n key), and
 * a status outside the map is shown as itself, never as "Draft".
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { FLOW_STATUS_DISPLAY, flowStatusDisplay, isFlowStatus } from "./flow-status.ts";

test("each known status keeps its own label", () => {
  assert.deepEqual(flowStatusDisplay("draft"), {
    status: "draft",
    label: "Draft",
    className: "text-default-secondary",
  });
  assert.equal(flowStatusDisplay("active").label, "Active");
  assert.equal(flowStatusDisplay("active").status, "active");
  assert.equal(flowStatusDisplay("archived").label, "Archived");
});

test("an unknown status is shown as itself, not as a draft", () => {
  const shown = flowStatusDisplay("paused");
  assert.equal(shown.status, null);
  assert.equal(shown.label, "paused");
  assert.notEqual(shown.label, FLOW_STATUS_DISPLAY.draft.label);
});

test("inherited object keys are not statuses", () => {
  for (const value of ["toString", "constructor", "__proto__", ""]) {
    assert.equal(isFlowStatus(value), false, value);
    assert.equal(flowStatusDisplay(value).status, null, value);
  }
});
