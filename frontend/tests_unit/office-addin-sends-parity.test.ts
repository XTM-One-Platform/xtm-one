/**
 * The Office add-in's typed sender (`apps/microsoft-office/src/lib/sends.ts`) and
 * the chat page's host sender (`src/components/chat-panel/host-send.ts`) keep
 * the composer the same way: the pane cannot import the page's module, so each
 * case below runs through both and must leave the composer as the other does.
 * A fix to one sender that the other lacks fails here.
 *
 * Run with `yarn test:unit` (Node's built-in runner).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { createTypedSender, type HeldOutcome, type SendOutcome } from "../../apps/microsoft-office/src/lib/sends.ts";
import { createHostSender } from "../src/components/chat-panel/host-send.ts";

interface Case {
  name: string;
  /** The composer before the send. */
  draft: string;
  /** What the send answers, or throws. */
  outcome: SendOutcome | "throws";
  /** The composer while the send is on its way (the user typed on). */
  typedMeanwhile?: string;
  /** How a held send settles. */
  settled?: HeldOutcome;
}

const TYPED = "summarise this section";

const CASES: Case[] = [
  { name: "a send that lands clears the text it stood for", draft: TYPED, outcome: "sent" },
  { name: "a steer that lands clears it too", draft: TYPED, outcome: "steered" },
  { name: "text typed meanwhile stays when the send lands", draft: TYPED, outcome: "sent", typedMeanwhile: `${TYPED} and more` },
  { name: "a dropped send puts its text back in an empty composer", draft: "", outcome: "dropped" },
  { name: "a failed send puts its text back after what was typed since", draft: "", outcome: "failed", typedMeanwhile: "next question" },
  { name: "a send that throws counts as failed", draft: "", outcome: "throws" },
  { name: "a lost send already in the composer is not written twice", draft: TYPED, outcome: "dropped" },
  { name: "a held send that leaves clears its text", draft: TYPED, outcome: "held", settled: "sent" },
  { name: "a held send that fails puts its text back", draft: "", outcome: "held", settled: "failed" },
  { name: "a held send returned to the composer puts its text back", draft: "", outcome: "held", settled: "returned" },
  { name: "a held send cancelled with its conversation leaves the composer alone", draft: "", outcome: "held", settled: "cancelled" },
];

function fakePanel(draft: string) {
  const panel = {
    draft,
    getDraft: () => panel.draft,
    setDraft: (text: string) => {
      panel.draft = text;
    },
  };
  return panel;
}

async function answer(c: Case, panel: { draft: string }): Promise<SendOutcome> {
  if (c.typedMeanwhile !== undefined) panel.draft = c.typedMeanwhile;
  if (c.outcome === "throws") throw new Error("the panel could not send");
  return c.outcome;
}

/** The composer the Office pane leaves. */
async function throughOffice(c: Case): Promise<string> {
  const panel = fakePanel(c.draft);
  const sender = createTypedSender(() => panel);
  await sender.send(TYPED, () => answer(c, panel));
  if (c.settled) sender.onHeldSendSettled(TYPED, c.settled);
  return panel.draft;
}

/** The composer the chat page leaves, for the same send of what was typed. */
async function throughPage(c: Case): Promise<string> {
  const panel = fakePanel(c.draft);
  const sender = createHostSender(() => ({ ...panel, getDraft: panel.getDraft, setDraft: panel.setDraft, send: () => answer(c, panel) }));
  await sender.send(TYPED, undefined, { composer: TYPED });
  if (c.settled) sender.onHeldSendSettled(TYPED, c.settled);
  return panel.draft;
}

for (const c of CASES) {
  test(`the pane and the chat page agree: ${c.name}`, async () => {
    assert.equal(await throughOffice(c), await throughPage(c));
  });
}
