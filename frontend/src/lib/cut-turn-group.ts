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
 * The replies a cut turn leaves in a transcript, folded into one event.
 *
 * A turn cut by a platform stop stores an interrupted reply
 * (`interrupted: true`, its tool trace kept); its automatic resume runs with
 * no new user message and ends with a reply carrying `resumed_from`. A resume
 * cut again stores another interrupted reply in the same place. The
 * transcript draws the whole sequence as ONE card (`InterruptedReplyCard`):
 * the interrupted replies, then the reply that resumed them when there is
 * one. A message the person sends after the cut ("Continue", the card's
 * Resume) resumes the same turn: the group stays open across it, the reply
 * that follows is its resume and the card says "resumed by you", never
 * "by itself" as well. A reply carrying `resumed_from` is the platform's
 * resume whatever the person sent meanwhile (a steer folded into the
 * running resume): the card then says "resumed by itself". Read off what
 * every message already serves, so messages stored before the card render
 * the same way.
 */
import type { ChatMessage, ResumedFromNote } from "@/types";

/** Consecutive interrupted replies of one turn, and the reply that resumed them. */
interface CutGroup {
  /** The interrupted replies, oldest first (at least one). */
  replies: ChatMessage[];
  /** The reply the resume ended with, when it ended. */
  resumedBy: ChatMessage | null;
  /**
   * The person sent a message after the newest cut: the resume is theirs.
   * A cut stored after that message clears it (their resume was cut too).
   */
  byUser: boolean;
}

/**
 * - `resumed`: the resume answered.
 * - `resuming`: a resume is running now.
 * - `queued`: the platform queued the resume; it starts by itself.
 * - `stopped`: could not resume by itself (a cause it does not resume, the
 *   cap reached): the warning state, the card holds Resume and Dismiss.
 * - `dismissed`: not resumed and the person dismissed it (or the
 *   conversation no longer serves the interrupted turn): a muted line.
 */
type CutState = "resumed" | "resuming" | "queued" | "stopped" | "dismissed";

export interface CutGroupView extends CutGroup {
  state: CutState;
  /** Who resumed it (`resumed` and `resuming` only): the platform or the person. */
  by: "itself" | "you" | null;
  /** The newest resume note: the live one, else the latest stored one. */
  note: ResumedFromNote | null;
  /** `stop`, `recycle` or null when the transcript does not say. */
  cause: string | null;
}

export interface CutGroups {
  /** Keyed by the id of the group's first interrupted reply. */
  byFirstId: Map<string, CutGroup>;
  /** Interrupted replies drawn by the card of an earlier one. */
  folded: Set<string>;
  /** Resumed replies whose own marker the card above already says. */
  resumedIds: Set<string>;
  /** The group the transcript ends with, nothing after it. */
  trailing: CutGroup | null;
}

/** Group the cut replies of a transcript (hidden rows already left out). */
export function groupCutReplies(messages: readonly ChatMessage[]): CutGroups {
  const byFirstId = new Map<string, CutGroup>();
  const folded = new Set<string>();
  const resumedIds = new Set<string>();
  let open: CutGroup | null = null;
  let last: CutGroup | null = null;
  for (const msg of messages) {
    if (msg.role === "assistant" && msg.interrupted && !msg.agent_switch) {
      if (open) {
        // The same turn cut again (by itself or after the person's message).
        // A resume of the person cut again is not "resumed by you": nothing
        // answered, so the group falls back to the live, queued, served or
        // not served state until a new message or a reply picks it up.
        open.replies.push(msg);
        open.byUser = false;
        folded.add(msg.id);
      } else {
        open = { replies: [msg], resumedBy: null, byUser: false };
        byFirstId.set(msg.id, open);
      }
      last = open;
      continue;
    }
    if (open && msg.role === "user") {
      // The person picked the cut turn up: its next reply is the resume.
      open.byUser = true;
      continue;
    }
    if (open && msg.role === "assistant" && !msg.agent_switch && (open.byUser || msg.resumed_from)) {
      open.resumedBy = msg;
      // The automatic resume answered: a message of the person sent while it
      // ran was a steer into it, not their resume.
      if (msg.resumed_from) open.byUser = false;
      resumedIds.add(msg.id);
    }
    open = null;
    last = null;
  }
  return { byFirstId, folded, resumedIds, trailing: last };
}

/** What stopped the process, read from a stored interrupted reply's first sentence. */
function causeFromContent(content: string | null | undefined): string | null {
  const head = (content ?? "").slice(0, 400);
  if (/\brecycled\b/.test(head)) return "recycle";
  if (/\bwas stopped\b/.test(head)) return "stop";
  return null;
}

/** The newest resume note of a group: on its resumed reply, else on a cut resume. */
function storedNote(group: CutGroup): ResumedFromNote | null {
  if (group.resumedBy?.resumed_from) return group.resumedBy.resumed_from;
  for (let i = group.replies.length - 1; i >= 0; i -= 1) {
    const note = group.replies[i].resumed_from;
    if (note) return note;
  }
  return null;
}

/**
 * The view of every group: its state, who resumed it, its newest note and
 * its cause. `live` is the note of the resume running now (`turn_resuming`),
 * `queued` that the conversation's interrupted turn waits for its automatic
 * resume, `served` that the conversation still serves the interrupted turn
 * (`interrupted_turn`, gone once dismissed); all three apply to the trailing
 * group only. A message of the person after the newest cut wins over any of
 * them but the live resume: the card says "resumed by you" from the moment
 * it is sent, unless the platform's resume is running (the message is then a
 * steer into it, and the card says "resuming by itself"); a cut stored after
 * that message hands the state back to them (`groupCutReplies`).
 *
 * `processing` is the conversation's own word that a turn is in flight
 * (`is_processing`): with no reply after the cut, that turn is its resume, so
 * the card says "resuming by itself" before the stream's `turn_resuming`
 * frame reaches a page just reloaded, and keeps saying it from the turn's end
 * until the stored transcript holds the reply that answered. The card never
 * reads "not resumed" for a turn resuming or just resumed (#10910): the
 * conversation serves no `interrupted_turn` while one is in flight.
 */
export function cutGroupViews(
  groups: CutGroups,
  {
    live,
    queued,
    served = true,
    processing = false,
  }: { live: ResumedFromNote | null; queued: boolean; served?: boolean; processing?: boolean },
): Map<string, CutGroupView> {
  const views = new Map<string, CutGroupView>();
  for (const [id, group] of groups.byFirstId) {
    const trailing = group === groups.trailing;
    const stored = storedNote(group);
    let state: CutState;
    let by: CutGroupView["by"] = null;
    let note = stored;
    if (group.resumedBy) {
      state = "resumed";
      by = group.byUser ? "you" : "itself";
    } else if (trailing && live) {
      state = "resuming";
      by = "itself";
      note = live;
    } else if (group.byUser) {
      state = "resumed";
      by = "you";
    } else if (trailing && processing) {
      state = "resuming";
      by = "itself";
    } else if (trailing && queued) state = "queued";
    else if (trailing && !served) state = "dismissed";
    else state = "stopped";
    const cause =
      note?.cause ?? causeFromContent(group.replies[group.replies.length - 1].content) ?? null;
    views.set(id, { ...group, state, by, note, cause });
  }
  return views;
}

/** One tool of the breakdown, in the order first called. */
interface ToolTally {
  name: string;
  calls: number;
  failed: number;
}

export interface CutBreakdown {
  tools: ToolTally[];
  /** Completed calls in all the group's replies. */
  completed: number;
  /** Calls started that had not returned when the last cut came. */
  running: number;
  /** The last completed call, and whether it failed. */
  last: { name: string; failed: boolean } | null;
}

/** The completed tool calls of a group, per tool, off the stored traces. */
export function cutBreakdown(replies: readonly ChatMessage[]): CutBreakdown {
  const tallies = new Map<string, ToolTally>();
  let completed = 0;
  let last: CutBreakdown["last"] = null;
  for (const reply of replies) {
    const trace = reply.tool_call_trace ?? [];
    if (trace.length === 0) {
      // A reply stored without a trace still names its tools.
      for (const name of reply.tool_names ?? []) {
        const tally = tallies.get(name) ?? { name, calls: 0, failed: 0 };
        tally.calls += 1;
        tallies.set(name, tally);
        completed += 1;
      }
      continue;
    }
    for (const entry of trace) {
      const failed = entry.success === false;
      const tally = tallies.get(entry.name) ?? { name: entry.name, calls: 0, failed: 0 };
      tally.calls += 1;
      tally.failed += failed ? 1 : 0;
      tallies.set(entry.name, tally);
      completed += 1;
      last = { name: entry.name, failed };
    }
  }
  const newest = replies[replies.length - 1];
  const started = newest?.tool_call_count ?? 0;
  const returned = newest?.tool_call_trace?.length ?? 0;
  return { tools: [...tallies.values()], completed, running: Math.max(0, started - returned), last };
}
