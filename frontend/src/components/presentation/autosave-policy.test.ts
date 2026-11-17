/**
 * Unit tests for when and how the slide editor saves on its own -
 * `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type { Presentation, PresentationSlide } from "@/types";

import {
  AUTOSAVE_IDLE_MS,
  AUTOSAVE_MAX_WAIT_MS,
  AUTOSAVE_RETRY_BASE_MS,
  AUTOSAVE_RETRY_MAX_MS,
  KEEPALIVE_BODY_MAX_BYTES,
  autosaveDelay,
  autosaveState,
  failureIsFinal,
  failureKind,
  keepaliveFits,
  retryDelay,
  saveBody,
} from "./autosave-policy.ts";
import { draftFrom } from "./presentation-editor.ts";

test("a save waits for the changes to pause, but never past the longest wait of a burst", () => {
  // One change: saved once the person pauses.
  assert.equal(autosaveDelay(1_000, 1_000, 1_000), AUTOSAVE_IDLE_MS);
  // Typing on: each keystroke pushes the save back...
  assert.equal(autosaveDelay(2_000, 2_000, 1_000), AUTOSAVE_IDLE_MS);
  // ...until the first change has waited the longest a burst may: saved now,
  // the person still typing.
  const first = 10_000;
  const now = first + AUTOSAVE_MAX_WAIT_MS - 100;
  assert.equal(autosaveDelay(now, now, first), 100);
  assert.equal(autosaveDelay(first + AUTOSAVE_MAX_WAIT_MS + 50, first + AUTOSAVE_MAX_WAIT_MS + 50, first), 0);
});

test("a save that did not reach the server is retried with a doubling delay, up to a cap", () => {
  assert.equal(retryDelay(1), AUTOSAVE_RETRY_BASE_MS);
  assert.equal(retryDelay(2), AUTOSAVE_RETRY_BASE_MS * 2);
  assert.equal(retryDelay(3), AUTOSAVE_RETRY_BASE_MS * 4);
  assert.equal(retryDelay(50), AUTOSAVE_RETRY_MAX_MS);
  // A 429's Retry-After is honoured, never shortened by the backoff.
  assert.equal(retryDelay(1, 45), 45_000);
  assert.equal(retryDelay(3, 1), AUTOSAVE_RETRY_BASE_MS * 4);
});

test("a save as the page goes away rides keepalive only while the browser accepts its size", () => {
  assert.equal(keepaliveFits({ slides: [{ title: "One" }] }), true);
  // Measured in bytes, not characters: a deck written in Japanese is three bytes a character.
  const characters = Math.ceil(KEEPALIVE_BODY_MAX_BYTES / 2);
  assert.equal(keepaliveFits({ text: "a".repeat(characters) }), true);
  assert.equal(keepaliveFits({ text: "あ".repeat(characters) }), false);
});

test("a failed save is read by what it means for the next one", () => {
  assert.equal(failureKind(409), "conflict");
  assert.equal(failureKind(422), "invalid");
  assert.equal(failureKind(400), "invalid");
  assert.equal(failureKind(403), "denied");
  assert.equal(failureKind(404), "denied");
  // An ended session is not a transport failure: retrying it forever cannot succeed.
  assert.equal(failureKind(401), "unauthorized");
  // A 429 stays a retry, after its Retry-After (`retryDelay`).
  for (const status of [undefined, 0, 429, 500, 502, 503]) assert.equal(failureKind(status), "transport");
});

test("only a refused right or an ended session stops the retries until the person acts", () => {
  assert.equal(failureIsFinal("unauthorized"), true);
  assert.equal(failureIsFinal("denied"), true);
  for (const kind of ["transport", "invalid", "conflict", "saved", "skipped", null, undefined]) {
    assert.equal(failureIsFinal(kind), false, String(kind));
  }
});

test("the status line says where the work stands, a refusal of an older draft no longer standing", () => {
  const base = { dirty: true, saving: false, outOfDate: false, online: true, failure: null, failureOnDraft: false };
  assert.equal(autosaveState({ ...base, dirty: false }), "saved");
  assert.equal(autosaveState(base), "pending");
  assert.equal(autosaveState({ ...base, saving: true }), "saving");
  assert.equal(autosaveState({ ...base, outOfDate: true }), "syncing");
  // A merge held while nothing is dirty is still on its way.
  assert.equal(autosaveState({ ...base, dirty: false, outOfDate: true }), "syncing");
  assert.equal(autosaveState({ ...base, online: false }), "offline");
  assert.equal(autosaveState({ ...base, failure: "transport" }), "retrying");
  assert.equal(autosaveState({ ...base, failure: "invalid", failureOnDraft: true }), "invalid");
  // The person changed the draft since the refusal: the next save is due.
  assert.equal(autosaveState({ ...base, failure: "invalid", failureOnDraft: false }), "pending");
  assert.equal(autosaveState({ ...base, failure: "denied" }), "denied");
  assert.equal(autosaveState({ ...base, failure: "unauthorized" }), "unauthorized");
  // Offline or not, an ended session is what the line says.
  assert.equal(autosaveState({ ...base, online: false, failure: "unauthorized" }), "unauthorized");
});

test("the save body carries the edited pieces and the revision it extends, the template and chrome only when changed", () => {
  const presentation = {
    id: "deck-1",
    title: "Deck",
    revision: 4,
    template_container_id: "tpl-1",
    spec: {
      title: "Deck",
      theme: "dark",
      aspect_ratio: "16:9",
      transition: "none",
      chrome: { show_footer: true },
      slides: [{ id: "a", layout: "title", title: "One" } as PresentationSlide],
    },
  } as unknown as Presentation;
  const draft = draftFrom(presentation);
  const body = saveBody(presentation, draft, 4);
  assert.equal(body.id, "deck-1");
  assert.equal(body.expected_revision, 4);
  assert.deepEqual(body.slides, draft.slides);
  // Unchanged: neither travels (the server reads the template's PRESENCE as a relink).
  assert.equal("template_container_id" in body, false);
  assert.equal("chrome" in body, false);

  const relinked = saveBody(presentation, { ...draft, template_container_id: null, chrome: { show_footer: false } }, 4);
  assert.equal(relinked.template_container_id, null);
  assert.deepEqual(relinked.chrome, { show_footer: false });
  // A blanked title keeps the stored one.
  assert.equal(saveBody(presentation, { ...draft, title: "   " }, 4).title, "Deck");
});
