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
 * What a host sends through the panel's handle (`send`), so the text never goes
 * missing: a send resolves `'held'` while a turn runs and only says later, through
 * `onHeldSendSettled`, whether it left. Text the composer holds for the send
 * stays there until the send lands; text that goes nowhere ends up in the composer.
 */
import { useState, type RefObject } from "react";
import type { ChatHeldOutcome, ChatPanelHandle, ChatSendOptions, ChatSendOutcome } from "@filigran/chatbot";

type PanelHandle = Pick<ChatPanelHandle, "send" | "getDraft" | "setDraft">;

interface HostSendText {
  /** The composer's text this send stands for: cleared once it lands, if the composer still holds exactly it. */
  composer?: string;
  /** What goes back to the composer when the send goes nowhere (the text sent by default). */
  restore?: string;
}

export interface HostSender {
  send: (text: string, options?: ChatSendOptions, where?: HostSendText) => Promise<ChatSendOutcome | undefined>;
  /** Hand to the panel (`onHeldSendSettled`). */
  onHeldSendSettled: (text: string, outcome: ChatHeldOutcome) => void;
}

interface Pending {
  composer?: string;
  restore: string;
}

export function createHostSender(panel: () => PanelHandle | null | undefined): HostSender {
  // The panel holds one message at a time, the sends held together settling together.
  let held: Pending[] = [];

  const landed = ({ composer }: Pending) => {
    const handle = panel();
    if (handle && composer !== undefined && handle.getDraft() === composer) handle.setDraft("");
  };
  const lost = ({ restore }: Pending) => {
    const handle = panel();
    if (!handle || !restore.trim()) return;
    const draft = handle.getDraft();
    if (draft.includes(restore)) return;
    handle.setDraft(draft.trim() ? `${draft}\n\n${restore}` : restore);
  };

  return {
    async send(text, options, where) {
      const handle = panel();
      if (!handle) return undefined;
      const pending: Pending = { composer: where?.composer, restore: where?.restore ?? text };
      let outcome: ChatSendOutcome;
      try {
        outcome = await handle.send(text, options);
      } catch {
        outcome = "failed";
      }
      if (outcome === "sent" || outcome === "steered") landed(pending);
      else if (outcome === "held") held.push(pending);
      else lost(pending);
      return outcome;
    },
    onHeldSendSettled(_text, outcome) {
      const settled = held;
      held = [];
      // Cancelled with its conversation or the panel: the composer is no longer its own.
      if (outcome === "cancelled") return;
      for (const pending of settled) {
        if (outcome === "sent") landed(pending);
        else lost(pending);
      }
    },
  };
}

/** {@link createHostSender} over a mounted panel's handle; stable for the host's lifetime. */
export function useHostSender(panelRef: RefObject<ChatPanelHandle | null>): HostSender {
  const [sender] = useState(() => createHostSender(() => panelRef.current));
  return sender;
}
