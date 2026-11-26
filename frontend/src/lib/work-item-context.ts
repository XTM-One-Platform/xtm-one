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
 * How the work-item views word what the server says about an item:
 * its `context`, built by `services/work_item_context.py`. The server decides
 * who may be named, why the reader sees the item and what will pick it up;
 * this module only picks the words and tones. Pure, so `node --test` reads it.
 */
import type {
  WorkItem,
  WorkItemBlockedRef,
  WorkItemBlockerRef,
  WorkItemFleetImpactKind,
  WorkItemPickupState,
  WorkItemPriority,
  WorkItemStatus,
} from "@/types";

/** What an item's completion changes for the agents it impacts. Read by `t_i18n(FLEET_IMPACT_KIND_LABELS[kind])`. @i18n-keys */
export const FLEET_IMPACT_KIND_LABELS = {
  skill: "Skill",
  persona: "Persona",
  assignment: "Assignment",
  runbook: "Runbook",
  retest: "Retest",
  state: "Shared state",
  capability: "Capability",
} as const satisfies Record<WorkItemFleetImpactKind, string>;

/** Read by `t_i18n(PICKUP_LABELS[state])`. @i18n-keys */
export const PICKUP_LABELS = {
  scheduled: "Runs at its time",
  finished: "Finished",
  in_progress: "Being worked on",
  blocked: "Blocked: not picked up",
  blocked_on_items: "Blocked on other items",
  waiting_for_refs: "Waiting for a change",
  waiting_for_person: "Waiting for a decision",
  waiting_for_ci: "Waiting for CI",
  unassigned: "Unassigned: nothing picks it up",
  automatic: "Picked up automatically",
  waiting_until_due: "Picked up once due",
  filtered_out: "Not picked up: filtered out",
  manual: "No automatic pickup",
} as const;

/**
 * A badge that carries a tooltip is a tab stop with a focus ring, so its
 * explanation reaches the keyboard as well as the mouse (as `InfoLabel` does).
 * `ring-2`, as the design system's focus ring on a small pill.
 */
export const BADGE_TIP_FOCUS = "cursor-help focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus";

/**
 * How many pull requests an item waiting for CI waits on: every one listed in
 * `pickup.pull_requests`, else one (the single `pickup.pull_request`).
 */
export function waitingPullRequestCount(pickup: { pull_requests?: unknown[] | null } | undefined): number {
  return pickup?.pull_requests?.length || 1;
}

export const PICKUP_TONE: Record<WorkItemPickupState, string> = {
  scheduled: "text-accent bg-accent/10",
  finished: "text-default-secondary bg-elevation-hover",
  in_progress: "text-filigran-ia-primary bg-filigran-ia-primary/10",
  blocked: "text-feedback-error-primary bg-feedback-error-primary/10",
  blocked_on_items: "text-accent bg-accent/10",
  waiting_for_refs: "text-accent bg-accent/10",
  waiting_for_person: "text-accent bg-accent/10",
  waiting_for_ci: "text-accent bg-accent/10",
  unassigned: "text-feedback-warning-tertiary bg-feedback-alert-primary/10",
  automatic: "text-feedback-success-primary bg-feedback-success-primary/10",
  waiting_until_due: "text-accent bg-accent/10",
  filtered_out: "text-feedback-warning-tertiary bg-feedback-alert-primary/10",
  manual: "text-feedback-warning-tertiary bg-feedback-alert-primary/10",
};

/**
 * What happens to the item, in a sentence. `{agent}` is the owner, `{date}`
 * when an inbox takes it, `{count}` how many pull requests it waits on
 * (`waitingPullRequestCount`). Read by
 * `t_i18n(pickupExplanation(state, canWrite), ...)`.
 * @i18n-keys
 */
const PICKUP_EXPLANATIONS = {
  scheduled: "The platform runs it at its time, with no assignment to set up.",
  finished: "Nothing acts on a finished item. Put it back in the queue to have it done again.",
  in_progress: "{agent} claimed it and is working on it. If nothing moves, put it back in the queue.",
  blocked: "Nothing picks up a blocked item until someone puts it back in the queue.",
  blocked_on_items: "It waits for the items below and goes back to the queue by itself once every one of them is done or one is cancelled.",
  waiting_for_refs: "It waits for the changes below and goes back to {agent} by itself once every one of them is released: nothing to do.",
  waiting_for_person: "It waits for a person's decision and goes back to {agent} by itself once the decision card is answered.",
  waiting_for_ci:
    "{count, plural, one {The CI of its pull request is running: when it completes, the platform hands the item to {agent} at once.} other {The CI of its # pull requests is running: when they complete, the platform hands the item to {agent} at once.}}",
  unassigned: "No owner agent is set, so nothing routes it to anyone. Complete it yourself, or ask an agent to take it over.",
  automatic: "An inbox drains the queue of {agent} and takes this item on the scheduler's next tick, within about a minute.",
  waiting_until_due: "An inbox drains the queue of {agent} and takes this item once it is due: {date}.",
  filtered_out: "{agent} has an inbox, but its filters leave this item out, so nothing picks it up automatically.",
  manual: "No assignment or flow drains the queue of {agent}, so nothing picks this item up automatically: it waits until that agent is asked to process its work items, or until someone completes it.",
} as const;

/**
 * The sentences of `PICKUP_EXPLANATIONS` that ask the reader to change the
 * item, said instead to a reader who may not change it (`can_write` false,
 * #1845): the dialog never advises an action it refuses.
 * @i18n-keys
 */
const READ_ONLY_PICKUP_EXPLANATIONS = {
  finished: "Nothing acts on a finished item. Someone who can change it must put it back in the queue to have it done again.",
  in_progress: "{agent} claimed it and is working on it. If nothing moves, someone who can change it must put it back in the queue.",
  unassigned: "No owner agent is set, so nothing routes it to anyone. Someone who can change it must complete it or assign an agent.",
} as const satisfies Partial<Record<WorkItemPickupState, string>>;

/**
 * The explanation of a pickup state for this reader: the read-only sentence
 * when the reader may not change the item and the state has one.
 */
export function pickupExplanation(state: WorkItemPickupState, canWrite: boolean | undefined, answered = false): string {
  if (answered && state === "waiting_for_person") return ANSWERED_PICKUP_EXPLANATIONS.waiting_for_person;
  const readOnly: Partial<Record<WorkItemPickupState, string>> = READ_ONLY_PICKUP_EXPLANATIONS;
  return (canWrite === false && readOnly[state]) || PICKUP_EXPLANATIONS[state];
}

/**
 * What an item waiting for a person says once a keeps_waiting option was
 * answered (#2500): the answer is recorded and the item keeps waiting, so the
 * dialog never says it goes back to its agent when the card is answered.
 * Read by `t_i18n(pickupExplanation(state, canWrite, answered), ...)`.
 * @i18n-keys
 */
const ANSWERED_PICKUP_EXPLANATIONS = {
  waiting_for_person: "Your answer is recorded; it keeps waiting until {agent} asks again.",
} as const satisfies Partial<Record<WorkItemPickupState, string>>;

/**
 * Whether the person already answered the current ask with an option that
 * keeps the item waiting (#1719, #2500): `payload.answer.option` names a
 * `keeps_waiting` option of the ask on an item that still needs a person.
 * The pickup badge, its explanation and "Waits for" then stop asking for a
 * decision. Pure.
 */
export function answeredKeepsWaiting(item: Pick<WorkItem, "needs_human" | "status" | "ask" | "payload">): boolean {
  if (!waitsForAnswer(item)) return false;
  const answer = (item.payload as { answer?: unknown } | null | undefined)?.answer;
  if (!answer || typeof answer !== "object") return false;
  const { option, answered_at: at } = answer as Record<string, unknown>;
  if (typeof option !== "string" || !option || typeof at !== "string" || !at) return false;
  const folded = option.toLowerCase();
  return !!item.ask?.options?.some((o) => o.keeps_waiting === true && o.key.toLowerCase() === folded);
}

/**
 * Whether the item waits for a person: nothing will act on it otherwise. The
 * states a row flags in the warning tone.
 */
export function waitsForSomeone(state: WorkItemPickupState): boolean {
  return state === "unassigned" || state === "filtered_out" || state === "manual" || state === "blocked";
}

/** A pull request a work item names (`payload.pull_requests`). */
export interface WorkItemPullRequest {
  repo: string;
  number: number;
  url: string | null;
  state: string;
}

/**
 * The pull requests the item's payload names, as the backend binds them
 * (`normalize_binding`): an `owner/name` repository and a positive integer
 * number (a digit string accepted), the repository lowercased, each named
 * once. Malformed entries are left out.
 */
export function pullRequestsOf(payload: Record<string, unknown> | null | undefined): WorkItemPullRequest[] {
  const entries = payload?.pull_requests;
  if (!Array.isArray(entries)) return [];
  const out: WorkItemPullRequest[] = [];
  const seen = new Set<string>();
  for (const entry of entries) {
    if (!entry || typeof entry !== "object") continue;
    const { repo: rawRepo, number: rawNumber, url, state } = entry as Record<string, unknown>;
    const number = typeof rawNumber === "string" && /^\d+$/.test(rawNumber) ? Number(rawNumber) : rawNumber;
    if (typeof rawRepo !== "string" || rawRepo.split("/").length !== 2) continue;
    if (typeof number !== "number" || !Number.isInteger(number) || number <= 0) continue;
    const repo = rawRepo.trim().toLowerCase();
    const key = `${repo}#${number}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      repo,
      number,
      url: typeof url === "string" && url.startsWith("https://") ? url : null,
      state: typeof state === "string" ? state : "open",
    });
  }
  return out;
}

/** The state a pull request chip shows: an unknown state reads as closed. */
export function pullRequestStateOf(state: string): "open" | "merged" | "closed" {
  if (state === "open" || state === "merged") return state;
  return "closed";
}

/** Read by `t_i18n(VISIBILITY_LABELS[reason])`. @i18n-keys */
export const VISIBILITY_LABELS = {
  own: "Yours",
  company_managed: "Fleet-wide",
  workspace: "Workspace",
} as const;

/** Why the reader sees the item. `{workspace}` is its workspace. @i18n-keys */
export const VISIBILITY_EXPLANATIONS = {
  own: "It was created for you: only you see it, with the agents acting for you.",
  company_managed: "A company-managed agent created it, so every member of the platform sees it. Only administrators and company-managed agents can change it.",
  workspace: "It is filed in the workspace {workspace}, which you can see.",
} as const;

/** How the item came to exist (`WorkItem.source`). @i18n-keys */
export const SOURCE_LABELS = {
  agent: "Created by an agent",
  manual: "Created through the API",
  action_register: "From an agent's Action Register",
  flow: "Created by a flow",
} as const;

export function sourceLabel(source: string): (typeof SOURCE_LABELS)[keyof typeof SOURCE_LABELS] {
  return SOURCE_LABELS[source as keyof typeof SOURCE_LABELS] ?? SOURCE_LABELS.agent;
}

/** A status as a person reads it. @i18n-keys
 *  "Open (status)": the catalogs read the bare "Open" as the verb ("Ouvrir"),
 *  a status is the adjective ("Ouvert"). */
export const STATUS_LABELS = {
  open: "Open (status)",
  claimed: "Claimed",
  in_progress: "In progress",
  blocked: "Blocked",
  done: "Done",
  cancelled: "Cancelled",
} as const satisfies Record<WorkItemStatus, string>;

/** A priority as a person reads it. @i18n-keys */
export const PRIORITY_LABELS = {
  low: "Low",
  medium: "Medium",
  high: "High",
  urgent: "Urgent",
} as const satisfies Record<WorkItemPriority, string>;

const TERMINAL_STATUSES: ReadonlySet<WorkItemStatus> = new Set(["done", "cancelled"]);

/**
 * Whether the item waits for *readerId* as the Waiting for me filter reads it
 * (services/work_item.py `_waits_for_person_clause`): a decision owed by the reader
 * (`needs_human`, its recipient the reader, else its user), or the reader's own item
 * no agent owns, open past its due date. A scheduled item never waits for anyone.
 */
export function waitsForReader(
  // A list row (`scheduled` its view) or a graph item node (`scheduled` a flag).
  item: Pick<WorkItem, "needs_human" | "waits_for_user_id" | "status" | "user_id" | "owner_agent_id" | "due_at"> & {
    scheduled: WorkItem["scheduled"] | boolean;
  },
  readerId: string | undefined,
  now: number = Date.now(),
): boolean {
  if (!readerId || item.scheduled || TERMINAL_STATUSES.has(item.status)) return false;
  if (item.needs_human) return (item.waits_for_user_id ?? item.user_id) === readerId;
  return (
    item.user_id === readerId && !item.owner_agent_id && item.status === "open" && !!item.due_at && Date.parse(item.due_at) < now
  );
}

/** Whether the item waits for a person's answer (#9437): its decision card shows. */
export function waitsForAnswer(item: Pick<WorkItem, "needs_human" | "status">): boolean {
  return !!item.needs_human && !TERMINAL_STATUSES.has(item.status);
}


/**
 * Whether "Put back in the queue" does something: the item is held, blocked or
 * finished. A scheduled item is moved through its own actions.
 */
export function canRequeue(item: Pick<WorkItem, "status" | "claimed_by_agent_id" | "scheduled">): boolean {
  if (item.scheduled) return false;
  return item.status !== "open" || !!item.claimed_by_agent_id;
}

/**
 * The items a blocked item still waits for: every `blocked_on` entry
 * not done or cancelled. One the reader cannot open has no status and counts
 * as unfinished: the platform reopens the item once it is done all the same.
 */
export function unfinishedBlockers(item: Pick<WorkItem, "context">): WorkItemBlockerRef[] {
  return (item.context?.blocked_on ?? []).filter((b) => !b.status || !TERMINAL_STATUSES.has(b.status));
}

/** The items it waits for that were cancelled: the platform hands it back to its owner (a legacy row may still show it). */
export function cancelledBlockers(item: Pick<WorkItem, "context">): WorkItemBlockerRef[] {
  return (item.context?.blocked_on ?? []).filter((b) => b.status === "cancelled");
}

/** Whether an item it waits for was cancelled: the platform hands it back to its owner (a legacy row may still show it). */
function hasCancelledBlocker(item: Pick<WorkItem, "context">): boolean {
  return cancelledBlockers(item).length > 0;
}

/**
 * The pickup states of a blocked item the platform releases by itself besides
 * its items: a reference, a person's answer. A blocked item waiting for CI keeps
 * its own "Blocked" chip and CI tooltip.
 */
const SELF_RELEASED: ReadonlySet<WorkItemPickupState> = new Set(["waiting_for_refs", "waiting_for_person"]);

/**
 * What a blocked item needs: `waiting` when the platform releases it by
 * itself (at least one item it depends on is unfinished and none was
 * cancelled; or the server says it waits for a reference or a person's
 * answer: #861), `action` otherwise (a scheduled leftover, a run cut by
 * a restart, a reason only, a cancelled dependency: someone puts it back in
 * the queue or closes it). `null` when the item is not blocked.
 */
export function blockedKind(item: Pick<WorkItem, "status" | "context">): "waiting" | "action" | null {
  if (item.status !== "blocked") return null;
  if (hasCancelledBlocker(item)) return "action";
  if (unfinishedBlockers(item).length > 0) return "waiting";
  const state = item.context?.pickup.state;
  return state && SELF_RELEASED.has(state) ? "waiting" : "action";
}

/**
 * The dependencies a blocked item's "Waits for" list and its pickup badge show:
 * only the unfinished ones when the item is waiting, so the badge count
 * equals the links printed next to it; every dependency otherwise (a cancelled
 * one is the reason it needs an action).
 */
export function listedBlockers(item: Pick<WorkItem, "status" | "context">): WorkItemBlockerRef[] {
  return blockedKind(item) === "waiting" ? unfinishedBlockers(item) : (item.context?.blocked_on ?? []);
}

/**
 * Why a blocked item needs an action, in words written for a person: its
 * result, when one was written. Never a claim note: the platform writes those
 * for the trail, in English, with item ids and field names ("still blocked:
 * <uuid> cancelled; ... change blocked_on"), so they are no reason to show in
 * a tooltip. A cancelled dependency is told by the chip itself
 * from `cancelledBlockers`. `null` when there is no result.
 */
export function blockedReason(item: Pick<WorkItem, "result">): string | null {
  const result = item.result?.trim();
  return result || null;
}

/** Whether a reference is satisfied, as `work_item_blocked_refs._satisfied` reads it. */
function refSatisfied(ref: WorkItemBlockedRef): boolean {
  if (ref.kind === "deployment") return ref.state === "deployed";
  if (ref.kind === "workflow_run") return ref.state === "succeeded";
  return ref.state === "merged" || ref.state === "closed";
}

/**
 * The references a blocked item still waits for: every `blocked_on_refs`
 * entry not satisfied yet (a pull request or issue not merged or closed, a
 * deployment not live, a workflow with no successful run). Empty when the
 * item is not blocked.
 */
export function pendingBlockedRefs(item: Pick<WorkItem, "status" | "blocked_on_refs">): WorkItemBlockedRef[] {
  if (item.status !== "blocked") return [];
  return (item.blocked_on_refs ?? []).filter((ref) => !refSatisfied(ref));
}

/**
 * `owner/name#12` for a pull request, an issue and the deployment of a pull
 * request; `owner/name@1a2b3c4` for the deployment of a commit;
 * `owner/name: release.yml` for a workflow run. The icon and the localised
 * kind of the accessible name carry the kind, so no English word lands in
 * the rendered text.
 */
export function blockedRefLabel(ref: Pick<WorkItemBlockedRef, "repo" | "number" | "sha" | "workflow" | "kind">): string {
  if (ref.kind === "workflow_run") return `${ref.repo}: ${ref.workflow ?? ""}`;
  if (ref.number != null) return `${ref.repo}#${ref.number}`;
  if (ref.sha) return `${ref.repo}@${ref.sha.slice(0, 7)}`;
  return ref.repo;
}

/**
 * What releases a reference, in a sentence a person reads: the explanation of
 * a waiting item names one per kind it waits for. Read by
 * `refReleaseSentences`. @i18n-keys
 */
const REF_RELEASE_EXPLANATIONS = {
  pull_request: "A pull request releases it once merged or closed.",
  issue: "An issue releases it once closed.",
  deployment: "A deployment releases it once the change runs in production.",
  workflow_run: "A workflow releases it at its next successful run.",
} as const satisfies Record<WorkItemBlockedRef["kind"], string>;

/**
 * The pull request sentence when every pull request waited for is released
 * by the same event (#842, J.2): "merged or closed" only when that is what the
 * references say. Read by `t_i18n(...)`. @i18n-keys
 */
const PULL_REQUEST_RELEASE = {
  merged: "A pull request releases it once merged.",
  closed: "A pull request releases it once closed.",
} as const;

/** The kinds of *refs*, each once, in the order `REF_RELEASE_EXPLANATIONS` lists them. */
function refKindsOf(refs: Pick<WorkItemBlockedRef, "kind">[]): WorkItemBlockedRef["kind"][] {
  const kinds = new Set(refs.map((ref) => ref.kind));
  return (Object.keys(REF_RELEASE_EXPLANATIONS) as WorkItemBlockedRef["kind"][]).filter((kind) => kinds.has(kind));
}

/**
 * One release sentence per kind of reference *refs* holds (never one per
 * reference: each reference stays a chip under "Waits for"), as untranslated
 * keys. The pull request sentence follows `release_on`: "merged" when every
 * pull request waits for its merge, "closed" when every one waits for its
 * close, "merged or closed" otherwise (#842, J.2).
 */
export function refReleaseSentences(
  refs: Pick<WorkItemBlockedRef, "kind" | "release_on">[],
): { kind: WorkItemBlockedRef["kind"]; text: string }[] {
  return refKindsOf(refs).map((kind) => {
    if (kind !== "pull_request") return { kind, text: REF_RELEASE_EXPLANATIONS[kind] };
    const events = new Set(refs.filter((ref) => ref.kind === "pull_request").map((ref) => ref.release_on));
    const only = events.size === 1 ? [...events][0] : null;
    const text = only === "merged" || only === "closed" ? PULL_REQUEST_RELEASE[only] : REF_RELEASE_EXPLANATIONS.pull_request;
    return { kind, text };
  });
}

/**
 * Whether a blocked item says nothing of what it waits for: no item,
 * no pull request or issue, no reason, no person. The server decides
 * (`blocked_why.missing`); the views show a "No reason" chip until it is edited.
 */
export function blockedWithoutReason(item: Pick<WorkItem, "status" | "blocked_why">): boolean {
  return item.status === "blocked" && !!item.blocked_why?.missing;
}

/**
 * Whether "Mark done" belongs on the row: not on an item that waits for open
 * dependencies or references (released by itself); an item waiting for a
 * person's decision keeps it, as before.
 */
export function offersMarkDone(item: Pick<WorkItem, "status" | "scheduled" | "context">): boolean {
  if (!canClose(item)) return false;
  return blockedKind(item) !== "waiting" || item.context?.pickup.state === "waiting_for_person";
}

/** Whether the item can still be completed or cancelled by hand. */
export function canClose(item: Pick<WorkItem, "status" | "scheduled">): boolean {
  return !item.scheduled && !TERMINAL_STATUSES.has(item.status);
}

/**
 * The payload as the agent wrote it, without the platform's own bookkeeping
 * (the dispatch attempt count, shown on its own), or `null` when empty.
 */
export function visiblePayload(payload: Record<string, unknown> | null | undefined): Record<string, unknown> | null {
  if (!payload) return null;
  const { _dispatch_attempts: _attempts, ...rest } = payload;
  return Object.keys(rest).length ? rest : null;
}
