/**
 * Unit tests for the event type name rule - `yarn test:unit`.
 *
 * The declare form enables Create only for a name the API accepts, so these
 * cases mirror `backend/tests/test_flow_event_type_update_audit.py`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  EVENT_TYPE_NAME_MAX_LENGTH,
  normalizeEventTypeName,
} from "./event-type-name.ts";

test("the limit is the column's", () => {
  assert.equal(EVENT_TYPE_NAME_MAX_LENGTH, 120);
});

test("a name with nothing visible normalizes to empty", () => {
  for (const blank of [
    "",
    "   ",
    "\t\n ",
    "\u200B",
    "\uFEFF",
    "\u200C\u200D",
    " \u2060 ",
    "\u00A0\u3000",
    "\u001C\u001F",
    " \u200B \uFEFF \u2060 ",
  ]) {
    assert.equal(normalizeEventTypeName(blank), "", JSON.stringify(blank));
  }
});

test("padding is stripped from both ends only", () => {
  assert.equal(normalizeEventTypeName("  incident  "), "incident");
  assert.equal(normalizeEventTypeName("\uFEFF incident\u200B\u3000"), "incident");
  assert.equal(normalizeEventTypeName("\u001Cincident\u001F"), "incident");
  assert.equal(normalizeEventTypeName("ticket escalated"), "ticket escalated");
});

test("invisible characters inside a name are content", () => {
  const technologist = "on-call \u{1F468}\u200D\u{1F4BB}";
  assert.equal(normalizeEventTypeName(technologist), technologist);
  const england =
    "Ops \u{1F3F4}\u{E0067}\u{E0062}\u{E0065}\u{E006E}\u{E0067}\u{E007F}";
  assert.equal(normalizeEventTypeName(england), england);
});
