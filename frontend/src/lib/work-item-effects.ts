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
 * What each hand action on a work item does to its owner agent (#9879), and
 * why a stale item needs a look. One wording for the detail dialog, the row
 * menu and the close dialog, so the person knows before clicking.
 */
import type { WorkItem, WorkItemStaleReason } from "@/types";

/** The effect of Mark done and Cancel on the owner agent. @i18n-keys */
export const CLOSE_EFFECTS = {
  done: "Closes the item: its owner agent does not run it again. Its completion still reaches the agents it impacts. If the problem is gone but the agent should check, use Ask the agent > It's solved instead.",
  cancelled: "Closes the item without completion work: its owner agent does not run it again and no completion reaches the agents it impacts.",
} as const;

/** The effect of Put back in the queue. @i18n-keys */
const REQUEUE = {
  effect: "Its owner agent runs it again at its next pickup, from the item and its notes. Any wait it had is cleared.",
} as const;
export const REQUEUE_EFFECT = REQUEUE.effect;

/** Why a stale item needs a look, by the server's `stale_reason`. @i18n-keys */
export const STALE_REASON_LABELS = {
  past_due: "Past its due date.",
  wait_never_releases: "Waits for a deployment the platform does not watch: nothing will ever release it. Ask the agent to re-check it.",
  ref_wait_quiet: "Its pull request, issue or deployment has seen no activity for days.",
  quiet: "Taken or blocked, but quiet for over a day.",
} as const satisfies Record<WorkItemStaleReason, string>;

/**
 * The stale badge's own text, by the server's `stale_reason`: the cause is
 * on the badge, never on hover only (UX round 1 of #9890, standards s5). The
 * tooltip keeps the long sentence and the advice. @i18n-keys
 */
const STALE_REASON_BADGES = {
  past_due: "Past due",
  wait_never_releases: "Never released",
  ref_wait_quiet: "Reference quiet",
  quiet: "Quiet for a day",
} as const satisfies Record<WorkItemStaleReason, string>;

/** The tooltip and badge of a stale item from an older server, which names no reason. @i18n-keys */
const STALE_FALLBACK = { label: "Needs attention.", badge: "Stale" } as const;

type Translate = (text: string, values?: Record<string, string>) => string;

/** The stale badge's tooltip: the server's reason, else the generic one. */
export function staleReasonText(t_i18n: Translate, item: Pick<WorkItem, "stale_reason">): string {
  const reason = item.stale_reason;
  return t_i18n(reason && reason in STALE_REASON_LABELS ? STALE_REASON_LABELS[reason] : STALE_FALLBACK.label);
}

/** The stale badge's text: the server's reason in a few words, else "Stale". */
export function staleBadgeText(t_i18n: Translate, item: Pick<WorkItem, "stale_reason">): string {
  const reason = item.stale_reason;
  return t_i18n(reason && reason in STALE_REASON_BADGES ? STALE_REASON_BADGES[reason] : STALE_FALLBACK.badge);
}
