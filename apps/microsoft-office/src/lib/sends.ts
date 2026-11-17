/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * What the pane sends through the panel's handle in place of what the user
 * typed (the whole document read first), so the typed text never goes missing:
 * it stays in the composer until the send lands, a send held while a turn runs
 * included (the panel says later, through `onHeldSendSettled`, whether it left).
 *
 * Pure and import-free: the frontend's unit tests read it
 * (`frontend/tests_unit/office-addin-sends.test.ts`) without the add-in's
 * dependencies.
 */

/** What became of a send (`ChatSendOutcome`). */
export type SendOutcome = "sent" | "steered" | "held" | "dropped" | "failed";
/** What became of a held send (`ChatHeldOutcome`). */
export type HeldOutcome = "sent" | "failed" | "dropped" | "returned" | "cancelled";

export interface DraftPanel {
  getDraft(): string;
  setDraft(text: string): void;
}

export interface TypedSender {
  /** Send through *send* for *typed*, which the composer holds meanwhile. */
  send(typed: string, send: () => Promise<SendOutcome | undefined>): Promise<SendOutcome | undefined>;
  /** Hand to the panel (`onHeldSendSettled`). */
  onHeldSendSettled(text: string, outcome: HeldOutcome): void;
}

export function createTypedSender(panel: () => DraftPanel | null | undefined): TypedSender {
  // The panel holds one message at a time, the sends held together settling together.
  let held: string[] = [];

  // Cleared only while the composer holds that text alone: a held message
  // comes back after what was typed since.
  const landed = (typed: string) => {
    const handle = panel();
    if (handle && handle.getDraft() === typed) handle.setDraft("");
  };
  const lost = (typed: string) => {
    const handle = panel();
    if (!handle || !typed.trim()) return;
    const draft = handle.getDraft();
    if (draft.includes(typed)) return;
    handle.setDraft(draft.trim() ? `${draft}\n\n${typed}` : typed);
  };

  return {
    async send(typed, send) {
      let outcome: SendOutcome | undefined;
      try {
        outcome = await send();
      } catch {
        outcome = "failed";
      }
      if (outcome === "sent" || outcome === "steered") landed(typed);
      else if (outcome === "held") held.push(typed);
      else lost(typed);
      return outcome;
    },
    onHeldSendSettled(_text, outcome) {
      const settled = held;
      held = [];
      // Cancelled with its conversation or the pane: the composer is no longer its own.
      if (outcome === "cancelled") return;
      for (const typed of settled) {
        if (outcome === "sent") landed(typed);
        else lost(typed);
      }
    },
  };
}
