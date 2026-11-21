// SPDX-License-Identifier: Apache-2.0

/**
 * What the bell and the companion inbox share about a notification kind, so
 * the two surfaces cannot disagree (`services/notification_feed.py`).
 */
import type { NotificationItem } from "@/types";

type Translate = (text: string, values?: Record<string, string | number>) => string;

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
 * The line under a notification's title. A waiting work item's subtitle is a
 * label, not a name, so it is translated here; every other kind carries a
 * name (an agent's) shown as the server sent it.
 */
export function notificationSubtitle(item: Pick<NotificationItem, "kind" | "subtitle">, t: Translate): string | null {
  if (item.kind === "work_item_waiting") return t("Work item");
  return item.subtitle;
}
