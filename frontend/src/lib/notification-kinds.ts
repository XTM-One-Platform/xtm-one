// SPDX-License-Identifier: Apache-2.0

/**
 * What the bell and the companion inbox share about a notification kind, so
 * the two surfaces cannot disagree (`services/notification_feed.py`).
 */
import type { NotificationDecisionSource, NotificationItem, NotificationKind } from "@/types";

type Translate = (text: string, values?: Record<string, string | number>) => string;

/** The former decision kinds, read as `decision` for one release (#842, S4). */
const DECISION_KIND_ALIASES = ["hitl", "flow_hitl", "work_item_waiting"] as const;

/** The source each former decision kind stood for. */
const ALIAS_SOURCE: Record<(typeof DECISION_KIND_ALIASES)[number], NotificationDecisionSource> = {
  hitl: "run",
  flow_hitl: "flow_run",
  work_item_waiting: "work_item",
};

/** The label of each decision source, translated by the caller. */
const SOURCE_LABEL: Record<NotificationDecisionSource, string> = {
  run: "Run",
  flow_run: "Flow",
  work_item: "Work item",
};

function isDecisionAlias(kind: NotificationKind): kind is (typeof DECISION_KIND_ALIASES)[number] {
  return (DECISION_KIND_ALIASES as ReadonlyArray<NotificationKind>).includes(kind);
}

/** Whether an entry is a human gate: the `decision` kind or one of its aliases. */
export function isDecisionKind(kind: NotificationKind): boolean {
  return kind === "decision" || isDecisionAlias(kind);
}

/** What a decision entry waits on; `null` for any other entry. */
export function decisionSource(item: Pick<NotificationItem, "kind" | "source">): NotificationDecisionSource | null {
  if (item.kind === "decision") return item.source ?? null;
  return isDecisionAlias(item.kind) ? ALIAS_SOURCE[item.kind] : null;
}

/**
 * A decision still waiting for its answer (a run, a flow or a work item; the
 * server's `UNREAD_UNTIL_ANSWERED_KINDS` sets `awaiting_answer`): pinned and
 * unread until answered, whatever was opened, dismissed or marked read (#991).
 */
export function isWaitingDecision(item: Pick<NotificationItem, "awaiting_answer">): boolean {
  return item.awaiting_answer === true;
}

/**
 * Whether opening an entry marks it read: every unread entry but a decision
 * still waiting, which stays unread until answered (#991). The bell and the
 * companion inbox both call it, so the two cannot drift.
 */
export function readsOnOpen(item: Pick<NotificationItem, "unread" | "awaiting_answer">): boolean {
  return item.unread && !isWaitingDecision(item);
}

/** Whether "Mark all read" has anything to read: an unread entry that is not a waiting decision. */
export function canMarkAllRead(items: ReadonlyArray<Pick<NotificationItem, "unread" | "awaiting_answer">>): boolean {
  return items.some((item) => readsOnOpen(item));
}

/**
 * Whether the bell badge takes the alert style: any decision still waiting
 * (a run, a flow or a work item), never an entry already answered (#991).
 */
export function hasUnreadApproval(items: ReadonlyArray<Pick<NotificationItem, "unread" | "awaiting_answer">>): boolean {
  return items.some((item) => item.unread && isWaitingDecision(item));
}

/**
 * The line under a notification's title. A decision reads "<source> · <who
 * asks>" (human-gates.md section 5), its source label translated here; every
 * other kind carries a name (an agent's) shown as the server sent it.
 */
export function notificationSubtitle(item: Pick<NotificationItem, "kind" | "subtitle" | "source">, t: Translate): string | null {
  const source = decisionSource(item);
  if (source === null) return item.subtitle;
  const label = t(SOURCE_LABEL[source]);
  // A legacy work item entry carried a fixed English label, never a name.
  const by = item.kind === "work_item_waiting" ? null : item.subtitle;
  return by ? t("{source} · {by}", { source: label, by }) : label;
}
