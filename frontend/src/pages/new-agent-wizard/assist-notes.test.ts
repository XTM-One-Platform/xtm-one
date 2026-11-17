/**
 * Unit tests for the wizard assistant's notes (AGENT_WIZARD_ASSIST) -
 * `yarn test:unit`. A note is a code the server sent: a known one reads as its
 * sentence, anything else - a code added later, a prototype key - as the
 * generic one, never raw.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { ASSIST_ERROR_LABELS, ASSIST_NOTE_LABELS, assistErrorLabel, assistNoteLabel } from "./assist-notes.ts";

test("a known code reads as its own sentence", () => {
  assert.equal(assistNoteLabel("knowledge_bases_unavailable"), ASSIST_NOTE_LABELS.knowledge_bases_unavailable);
});

test("an unknown code or a prototype key reads as the generic sentence", () => {
  for (const code of ["added_later", "toString", "constructor", "__proto__", ""]) {
    assert.equal(assistNoteLabel(code), ASSIST_NOTE_LABELS.unknown);
  }
});

test("a failed round with a known code reads as its own sentence; anything else does not", () => {
  const failed = (detail: unknown) => Object.assign(new Error("raw"), { status: 503, detail });
  assert.equal(
    assistErrorLabel(failed({ code: "assistant_model_unavailable", message: "x" })),
    ASSIST_ERROR_LABELS.assistant_model_unavailable,
  );
  assert.equal(
    assistErrorLabel(failed({ code: "assistant_no_playable_answer" })),
    ASSIST_ERROR_LABELS.assistant_no_playable_answer,
  );
  for (const detail of [undefined, "a string detail", { code: "toString" }, { code: 503 }, null]) {
    assert.equal(assistErrorLabel(failed(detail)), null);
  }
  assert.equal(assistErrorLabel(null), null);
});
