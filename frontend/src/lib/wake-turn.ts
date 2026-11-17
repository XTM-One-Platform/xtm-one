// SPDX-License-Identifier: Apache-2.0

import type { WakeNote } from "@/types";

/** `parent_wake.PARENT_WAKE_SOURCE`: the source a wake turn's note names. */
const WAKE_SOURCE = "background_task_wake";

/** The note a wake turn's `thinking` ack (or a message's `wake`) carries, when well-formed (#11796). */
export function readWakeNote(raw: unknown): WakeNote | null {
  if (!raw || typeof raw !== "object") return null;
  const note = raw as Record<string, unknown>;
  if (note.source !== WAKE_SOURCE) return null;
  return {
    source: WAKE_SOURCE,
    task_id: typeof note.task_id === "string" && note.task_id ? note.task_id : null,
    agent_name: typeof note.agent_name === "string" && note.agent_name ? note.agent_name : null,
    kind: typeof note.kind === "string" && note.kind ? note.kind : null,
  };
}
