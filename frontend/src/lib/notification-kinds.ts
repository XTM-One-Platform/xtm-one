// SPDX-License-Identifier: Apache-2.0

/**
 * What the bell and the companion inbox share about a notification kind, so
 * the two surfaces cannot disagree (`services/notification_feed.py`).
 */
import type { NotificationItem, NotificationKind } from "@/types";

type Translate = (text: string, values?: Record<string, string | number>) => string;

/**
 * Kinds unread until answered (`UNREAD_UNTIL_ANSWERED_KINDS` on the server):
 * marking one read does nothing, so neither surface offers it, and opening
 * one writes no read state.
 */
export const UNREAD_UNTIL_ANSWERED: ReadonlySet<NotificationKind> = new Set<NotificationKind>(["hitl", "work_item_waiting"]);

/**
 * The line under a notification's title. A waiting work item's subtitle is a
 * label, not a name, so it is translated here; every other kind carries a
 * name (an agent's) shown as the server sent it.
 */
export function notificationSubtitle(item: Pick<NotificationItem, "kind" | "subtitle">, t: Translate): string | null {
  if (item.kind === "work_item_waiting") return t("Work item");
  return item.subtitle;
}
