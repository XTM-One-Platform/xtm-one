// SPDX-License-Identifier: Apache-2.0

/**
 * What the bell and the companion inbox share about a notification kind, so
 * the two surfaces cannot disagree (`services/notification_feed.py`).
 */
import type { NotificationItem, NotificationKind } from "@/types";

type Translate = (text: string, values?: Record<string, string | number>) => string;

/**
 * Kinds unread until answered (`UNREAD_UNTIL_ANSWERED_KINDS` on the server):
 * "Mark all read" leaves them unread, so it is not offered for them alone.
 * Opening or dismissing one entry reads it (#901), see `readsOnOpen`.
 */
export const UNREAD_UNTIL_ANSWERED: ReadonlySet<NotificationKind> = new Set<NotificationKind>(["hitl", "work_item_waiting"]);

/**
 * Whether opening an entry marks it read: every unread entry, a pinned one
 * included, even when its target can no longer be opened (#901). The bell and
 * the companion inbox both call it, so the two cannot drift.
 */
export function readsOnOpen(item: Pick<NotificationItem, "unread">): boolean {
  return item.unread;
}

/**
 * Whether the bell badge takes the alert style: an approval still unread, never
 * the mere presence of an approval entry already read (#901).
 */
export function hasUnreadApproval(items: ReadonlyArray<Pick<NotificationItem, "kind" | "unread">>): boolean {
  return items.some((item) => item.unread && item.kind === "hitl");
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
