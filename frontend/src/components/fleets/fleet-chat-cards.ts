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
 * What the fleet's "Modify with AI" panel shows under the agent's last answer
 * (#10162, UX verdict 5dcb88e3 R8 and R9), read from the fleet tools' own
 * answers in the turn's `tool_call_trace`:
 *
 * - R8 confirm: `set_fleet_automations(action="pause")` without `confirm=true`
 *   answers "Not applied. Pause all would switch off N automation(s)..." and
 *   applies nothing (#10165). The panel shows that summary as a card, Cancel
 *   first, then the destructive "Apply N changes" that sends the user's
 *   agreement, so the agent calls again with `confirm=true`.
 * - R9 raise refusal: `update_fleet` refuses to raise the autonomy level
 *   (`_AUTONOMY_RAISE_REFUSAL`): the panel names it and offers the fleet
 *   page's own autonomy control, the one place a person raises it.
 *
 * Only the newest message counts, and only while it is the agent's: once the
 * user answered (or wrote anything else), the card is gone. The wordings
 * matched are the backend's (`builtin_tools/fleets.py`), kept stable by its
 * tests.
 */
import type { ChatMessage } from "@/types";

/** The tool that answers a pause summary until confirmed. */
const AUTOMATIONS_TOOL = "set_fleet_automations";
/** The tool that refuses an autonomy raise. */
const UPDATE_TOOL = "update_fleet";

const PAUSE_SUMMARY_RE = /^Not applied\. Pause all would switch off (\d+) automation/;
const RAISE_REFUSAL_RE = /^An agent can only lower a fleet's autonomy level\./;

export type FleetChatCard =
  | { kind: "confirm"; messageId: string; count: number }
  | { kind: "raise_refused"; messageId: string };

/** The card the panel shows under the transcript, or `null`. */
export function fleetChatCard(messages: readonly ChatMessage[]): FleetChatCard | null {
  const last = messages[messages.length - 1];
  if (!last || last.role !== "assistant") return null;
  const trace = last.tool_call_trace ?? [];
  // The newest fleet answer of the turn decides: a pause confirmed later in the same turn shows nothing.
  for (let i = trace.length - 1; i >= 0; i -= 1) {
    const entry = trace[i];
    if (entry.name === AUTOMATIONS_TOOL) {
      const match = PAUSE_SUMMARY_RE.exec(entry.output ?? "");
      return match ? { kind: "confirm", messageId: last.id, count: Number(match[1]) } : null;
    }
    if (entry.name === UPDATE_TOOL) {
      return RAISE_REFUSAL_RE.test(entry.output ?? "") ? { kind: "raise_refused", messageId: last.id } : null;
    }
  }
  return null;
}
