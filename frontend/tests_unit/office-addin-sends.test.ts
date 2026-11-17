/**
 * The Office add-in's typed sender (`apps/microsoft-office/src/lib/sends.ts`):
 * a question on the whole document is sent in place of what the user typed,
 * which stays in the composer until the send lands - held behind a running
 * turn included - and is never lost when the send goes nowhere.
 *
 * Run with `yarn test:unit` (Node's built-in runner).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { createTypedSender, type SendOutcome } from "../../apps/microsoft-office/src/lib/sends.ts";

function fakePanel(draft = "") {
  const panel = {
    draft,
    getDraft: () => panel.draft,
    setDraft: (text: string) => {
      panel.draft = text;
    },
  };
  return panel;
}

const TYPED = "summarize the whole document";
const answer = (outcome: SendOutcome | undefined) => async () => outcome;

test("the composer keeps the typed text until the send lands, then is cleared", async () => {
  for (const outcome of ["sent", "steered"] as const) {
    const panel = fakePanel(TYPED);
    await createTypedSender(() => panel).send(TYPED, answer(outcome));
    assert.equal(panel.draft, "");
  }
});

test("a send that goes nowhere leaves the typed text in the composer", async () => {
  for (const outcome of ["failed", "dropped", undefined] as const) {
    const panel = fakePanel(TYPED);
    await createTypedSender(() => panel).send(TYPED, answer(outcome));
    assert.equal(panel.draft, TYPED);
  }
  const panel = fakePanel(TYPED);
  await createTypedSender(() => panel).send(TYPED, async () => {
    throw new Error("the document could not be read");
  });
  assert.equal(panel.draft, TYPED);
});

test("a held send keeps the typed text until the panel says it left", async () => {
  const panel = fakePanel(TYPED);
  const sender = createTypedSender(() => panel);
  await sender.send(TYPED, answer("held"));
  assert.equal(panel.draft, TYPED);
  sender.onHeldSendSettled("the prompt", "sent");
  assert.equal(panel.draft, "");
});

test("a held send that Stop returns, or that fails or is dropped, leaves the typed text in the composer", async () => {
  for (const outcome of ["returned", "failed", "dropped"] as const) {
    const panel = fakePanel(TYPED);
    const sender = createTypedSender(() => panel);
    await sender.send(TYPED, answer("held"));
    // The user cleared the composer meanwhile: the text comes back.
    panel.draft = "";
    sender.onHeldSendSettled("the prompt", outcome);
    assert.equal(panel.draft, TYPED);
  }
});

test("text typed since stays, and the landed send clears only a composer holding its text alone", async () => {
  const panel = fakePanel(`typed since\n\n${TYPED}`);
  await createTypedSender(() => panel).send(TYPED, answer("sent"));
  assert.equal(panel.draft, `typed since\n\n${TYPED}`);
});
