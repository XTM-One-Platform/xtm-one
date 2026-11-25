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
 * Everything one work item says: who acts on it and whether anything
 * will, where it came from and for whom, why the reader sees it, its full
 * description, result and payload, and its timeline. Opened from a row or
 * from a link carrying `?item=<id>` (a run's "Scheduled" badge).
 *
 * Layout: the badges, then a compact grid of the item's facts (who
 * created it, where, for whom, its key, its time), then the description and
 * the result rendered as the Markdown agents write them, the files it carries, then who acts on it,
 * why the reader sees it, the payload and the timeline.
 */
import type { ElementType, ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Braces,
  CalendarClock,
  Clock,
  Eye,
  FileText,
  Hash,
  ListTodo,
  Paperclip,
  RotateCcw,
  Trash2,
  Workflow,
  XCircle,
  CheckCircle2,
  Zap,
} from "lucide-react";
import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { WorkItemActivity } from "@/components/work-items/WorkItemActivity";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { OutcomeReview } from "@/components/ui/outcome-review";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Tip } from "@/components/ui/tooltip";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { BlockedStatusBadge } from "@/components/work-items/BlockedStatusBadge";
import { WorkItemAskAgent, offersAskTheAgent } from "@/components/work-items/WorkItemAskAgent";
import { WorkItemCaseLink } from "@/components/work-items/WorkItemCaseLink";
import { WorkItemDecisionCard } from "@/components/work-items/WorkItemDecisionCard";
import { WorkItemFiles } from "@/components/work-items/WorkItemFiles";
import { CLOSE_EFFECTS, REQUEUE_EFFECT, staleBadgeText, staleReasonText } from "@/lib/work-item-effects";
import { ScheduleActions } from "@/components/work-items/ScheduleActions";
import { ScheduleStateBadge, ScheduledDetails } from "@/components/work-items/ScheduledDetails";
import {
  WorkItemAgent,
  WorkItemHoldingRunLink,
  WorkItemImpacts,
  WorkItemOriginLink,
  WorkItemPerson,
  WorkItemPickupBadge,
  WorkItemBlockers,
  WorkItemPullRequestChips,
  WorkItemVisibilityBadge,
  WorkItemBlockedWhy,
  WorkItemNoReasonChip,
} from "@/components/work-items/WorkItemContextParts";
import { useWorkItem } from "@/hooks/api/useWorkItemApi";
import { useFormatter } from "@/i18n";
import { isScheduledItem } from "@/lib/scheduled-work-items";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { cn } from "@/lib/utils";
import {
  FLEET_IMPACT_KIND_LABELS,
  pickupExplanation,
  PRIORITY_LABELS,
  STATUS_LABELS,
  VISIBILITY_EXPLANATIONS,
  blockedKind,
  blockedReason,
  blockedWithoutReason,
  pendingBlockedRefs,
  refReleaseSentences,
  canClose,
  offersMarkDone,
  cancelledBlockers,
  unfinishedBlockers,
  listedBlockers as listedBlockersOf,
  canRequeue,
  pullRequestsOf,
  sourceLabel,
  visiblePayload,
  waitingPullRequestCount,
  waitsForAnswer,
} from "@/lib/work-item-context";
import type { WorkItem, WorkItemContext, WorkItemInboxRef } from "@/types";
import { Spinner } from "@filigran/design-system";

/** What the page lets the reader do with an item; absent = not offered. */
export interface WorkItemActions {
  close?: (item: WorkItem, mode: "done" | "cancelled") => void;
  requeue?: (item: WorkItem) => void;
  remove?: (item: WorkItem) => void;
  /** The reader may answer the decision the item waits for (#9437). */
  answer?: boolean;
  busy?: boolean;
}

function Section({ icon: Icon, title, children }: { icon: ElementType; title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="flex items-center gap-1.5 content-caption font-medium uppercase tracking-wider text-default-secondary">
        <Icon className="h-3.5 w-3.5" /> {title}
      </h3>
      <div className="space-y-2 content-compact text-default-primary">{children}</div>
    </section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] items-start gap-3">
      <span className="text-default-secondary">{label}</span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function InboxLine({ inbox }: { inbox: WorkItemInboxRef }) {
  const { t_i18n } = useFormatter();
  const Icon = inbox.kind === "flow" ? Workflow : Zap;
  const name = inbox.accessible && inbox.id ? (
    <Link
      to={inbox.kind === "flow" ? `/flows/library/${inbox.id}` : `/agents/assignments/${inbox.id}`}
      className="text-accent hover:underline"
    >
      {inbox.name}
    </Link>
  ) : (
    <span className="italic text-default-secondary">
      {inbox.kind === "flow" ? t_i18n("a flow you cannot open") : t_i18n("an assignment you cannot open")}
    </span>
  );
  const filters = [
    inbox.priority_min && t_i18n("priority {priority} and above", { priority: t_i18n(PRIORITY_LABELS[inbox.priority_min]) }),
    inbox.correlation_prefix && t_i18n("correlation keys starting with {prefix}", { prefix: inbox.correlation_prefix }),
  ].filter(Boolean);
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1" data-testid="work-item-inbox">
      <Icon className={cn("h-3.5 w-3.5", inbox.accepts ? "text-feedback-success-primary" : "text-feedback-warning-primary")} />
      {name}
      <span className={inbox.accepts ? "text-feedback-success-primary" : "text-feedback-warning-primary"}>
        {inbox.accepts ? t_i18n("takes this item") : t_i18n("leaves this item out")}
      </span>
      {filters.length > 0 && (
        <span className="text-default-secondary">({filters.join(", ")})</span>
      )}
    </li>
  );
}

function WhoActs({ item, context }: { item: WorkItem; context: WorkItemContext }) {
  const { t_i18n, intl } = useFormatter();
  // Dates in the app language, never the browser's or English.
  const formatShortDateTime = (at: string) => formatShortDateTimeIntl(intl, at);
  const { pickup } = context;
  const owner = context.owner_agent;
  const agentWord = owner?.accessible && owner.name ? owner.name : t_i18n("an agent you cannot open");
  // The platform's latest note on the claim: who released it, who took it over.
  const latestNote = item.claim_notes?.length ? item.claim_notes[item.claim_notes.length - 1] : null;
  const autoBlocked =
    item.status === "blocked" && pickup.dispatch_attempts >= pickup.max_dispatch_attempts && pickup.dispatch_attempts > 0;
  const pullRequests = pullRequestsOf(item.payload);
  // The badge counts what the "Waits for" list shows.
  const listedBlockers = item.status === "blocked" ? listedBlockersOf(item) : [];
  return (
    <Section icon={Zap} title={t_i18n("Who acts on it")}>
      <Field label={t_i18n("Owner agent")}>
        {owner ? <WorkItemAgent agent={owner} /> : <span className="text-default-secondary">{t_i18n("Nobody")}</span>}
      </Field>
      {context.fleet_impact?.agents.length ? (
        // The agents its completion is routed to: who, what it changes for them, what to do.
        <Field label={t_i18n("Impacts")}>
          <div className="space-y-1" data-testid="work-item-fleet-impact">
            <span className="inline-flex flex-wrap items-center gap-x-2">
              <WorkItemImpacts impact={context.fleet_impact} />
              {context.fleet_impact.kind && (
                <Badge variant="outline" className="content-caption">
                  {t_i18n(FLEET_IMPACT_KIND_LABELS[context.fleet_impact.kind])}
                </Badge>
              )}
            </span>
            {context.fleet_impact.instructions && (
              <p className="break-words text-default-secondary">{context.fleet_impact.instructions}</p>
            )}
            <p className="text-default-secondary">
              {context.fleet_impact.inferred
                ? t_i18n("Read from the follow-through section of its description: its completion is routed to its creator.")
                : t_i18n("Its completion is routed to these agents too.")}
            </p>
          </div>
        </Field>
      ) : null}
      {context.claimed_by_agent && (
        <Field label={t_i18n("Claimed by")}>
          <span className="inline-flex flex-wrap items-center gap-x-2">
            <WorkItemAgent agent={context.claimed_by_agent} />
            {item.claimed_at && (
              <span className="text-default-secondary">{t_i18n("since {date}", { date: formatShortDateTime(item.claimed_at) })}</span>
            )}
          </span>
        </Field>
      )}
      {item.claimed_by_run && (item.status === "claimed" || item.status === "in_progress") && (
        <Field label={t_i18n("Held by")}>
          <WorkItemHoldingRunLink run={item.claimed_by_run} />
        </Field>
      )}
      {latestNote && (
        <Field label={t_i18n("Claim note")}>
          <span className="inline-flex flex-wrap items-baseline gap-x-2" data-testid="work-item-claim-note">
            <span className="text-default-secondary">{formatShortDateTime(latestNote.at)}</span>
            <span className="break-words">{latestNote.text}</span>
          </span>
        </Field>
      )}
      <div
        className={cn(
          "flex items-start gap-2 rounded-sm border px-3 py-2",
          pickup.state === "automatic" ||
          pickup.state === "waiting_until_due" ||
          pickup.state === "blocked_on_items" ||
          pickup.state === "waiting_for_refs" ||
          pickup.state === "waiting_for_person" ||
          pickup.state === "waiting_for_ci"
            ? "border-feedback-success-primary/30 bg-feedback-success-primary/5"
            : "border-feedback-alert-primary/30 bg-feedback-alert-primary/5",
        )}
      >
        <WorkItemPickupBadge
          state={pickup.state}
          blockedOn={listedBlockers.length || undefined}
          waiting={blockedKind(item) === "waiting"}
          pullRequest={pickup.pull_request}
          pullRequests={pickup.pull_requests}
        />
        <p className="min-w-0 flex-1">
          {t_i18n(pickupExplanation(pickup.state, item.can_write), {
            agent: agentWord,
            date: pickup.available_at ? formatShortDateTime(pickup.available_at) : "",
            count: waitingPullRequestCount(pickup),
          })}
          {pickup.state === "waiting_for_refs" &&
            refReleaseSentences(pendingBlockedRefs(item)).map(({ kind, text }) => (
              <span key={kind} className="block" data-testid="work-item-ref-release" data-kind={kind}>
                {t_i18n(text)}
              </span>
            ))}
          {pickup.state === "waiting_for_ci" && item.status === "blocked" && (
            <>
              {" "}
              {t_i18n(
                "{count, plural, one {It stays blocked until the CI of its pull request completes and the platform hands it over.} other {It stays blocked until the CI of its # pull requests completes and the platform hands it over.}}",
                { count: waitingPullRequestCount(pickup) },
              )}
            </>
          )}
        </p>
      </div>
      {pullRequests.length > 0 && (
        <Field label={t_i18n("Pull requests")}>
          <WorkItemPullRequestChips pullRequests={pullRequests} />
        </Field>
      )}
      {/* Every unfinished blocker item, whatever the pickup state (a person's decision too). */}
      {listedBlockers.length ? (
        <Field label={t_i18n("Waits for")}>
          <WorkItemBlockers blockers={listedBlockers} />
        </Field>
      ) : null}
      {/* The pull requests, issues, reason or person it waits for. */}
      {item.status === "blocked" && (pendingBlockedRefs(item).length || item.blocked_reason?.trim() || item.needs_human) ? (
        <Field label={listedBlockers.length ? t_i18n("Also waits for") : t_i18n("Waits for")}>
          <WorkItemBlockedWhy refs={pendingBlockedRefs(item)} reason={item.blocked_reason} needsHuman={!!item.needs_human} />
        </Field>
      ) : null}
      {autoBlocked && (
        <p className="flex items-start gap-1.5 text-feedback-warning-primary">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {t_i18n("Blocked automatically after {count, plural, one {# failed dispatch attempt} other {# failed dispatch attempts}}.", { count: pickup.dispatch_attempts })}
        </p>
      )}
      {pickup.inboxes.length > 0 && (
        <ul className="space-y-1">
          {pickup.inboxes.map((inbox, index) => (
            <InboxLine key={inbox.id ?? `hidden-${index}`} inbox={inbox} />
          ))}
        </ul>
      )}
      {pickup.auto_cancel_at && (
        <p className="flex items-start gap-1.5 text-default-secondary">
          <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {t_i18n("If nobody acts on it, it is cancelled automatically around {date}.", {
            date: formatShortDateTime(pickup.auto_cancel_at),
          })}
        </p>
      )}
      {pickup.purge_at && (
        <p className="flex items-start gap-1.5 text-default-secondary">
          <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {t_i18n("Deleted automatically around {date}.", { date: formatShortDateTime(pickup.purge_at) })}
        </p>
      )}
    </Section>
  );
}

/**
 * One fact of the header grid: a short label over its value. The value stays
 * on one line inside its column, cut with an ellipsis; *full* is the
 * whole value, shown in a tooltip, so a long assignment name or chat title
 * never runs under the next column. A link inside stays a link.
 */
function Fact({ label, children, full, wide }: { label: string; children: ReactNode; full?: string; wide?: boolean }) {
  return (
    <div className={cn("min-w-0", wide && "col-span-2")}>
      <dt className="content-caption text-default-secondary">{label}</dt>
      <Tip content={full}>
        <dd className="flex min-w-0 items-center whitespace-nowrap text-default-primary" data-testid="work-item-fact-value">
          {children}
        </dd>
      </Tip>
    </div>
  );
}

/** The words the "Created in" value shows, whole, for its tooltip. */
function originText(origin: WorkItemContext["origin"], t_i18n: (key: string) => string): string | undefined {
  if (origin.flow_run) {
    return origin.flow_run.accessible ? (origin.flow_run.name ?? t_i18n("Flow run")) : t_i18n("a flow run you cannot open");
  }
  if (origin.run) return origin.run.accessible ? (origin.run.name ?? t_i18n("Agent run")) : t_i18n("a run you cannot open");
  if (origin.conversation) return origin.conversation.title || t_i18n("A chat");
  if (origin.conversation_hidden) return t_i18n("another person's chat");
  return undefined;
}

/** Where the item came from, for whom, its key and its time, at a glance. */
function Facts({ item, context }: { item: WorkItem; context: WorkItemContext | undefined }) {
  const { t_i18n, intl } = useFormatter();
  // The relative time and dates in the app language (A3 and A4).
  const formatShortDateTime = (at: string) => formatShortDateTimeIntl(intl, at);
  const recorded = !!(
    context &&
    (context.origin.conversation || context.origin.conversation_hidden || context.origin.run || context.origin.flow_run)
  );
  const creator = context?.created_by_agent;
  const notRecorded =
    item.source === "manual"
      ? t_i18n("A request to the work items API")
      : t_i18n("Not recorded: it was created before work items kept their origin.");
  const person = context?.person;
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-2 content-compact sm:grid-cols-3" data-testid="work-item-facts">
      <Fact
        label={t_i18n("Created by")}
        full={creator ? (creator.accessible && creator.name ? creator.name : undefined) : t_i18n(sourceLabel(item.source))}
      >
        {creator ? (
          <WorkItemAgent agent={creator} />
        ) : (
          <span className="truncate">{t_i18n(sourceLabel(item.source))}</span>
        )}
      </Fact>
      <Fact
        label={t_i18n("Created in")}
        wide={!recorded}
        full={recorded && context ? originText(context.origin, t_i18n) : notRecorded}
      >
        {recorded && context ? (
          <WorkItemOriginLink origin={context.origin} />
        ) : (
          <span className="truncate text-default-secondary">{notRecorded}</span>
        )}
      </Fact>
      {person && (
        <Fact label={t_i18n("On behalf of")} full={person.is_you ? t_i18n("you") : person.name || t_i18n("a deleted account")}>
          <WorkItemPerson person={person} />
        </Fact>
      )}
      {item.correlation_key && (
        <Fact label={t_i18n("Correlation key")} full={item.correlation_key}>
          <span className="inline-flex max-w-full items-center gap-1 font-mono-plex">
            <Hash className="h-3 w-3 shrink-0" /> <span className="truncate">{item.correlation_key}</span>
            <CopyIconButton value={item.correlation_key} label={t_i18n("Copy the correlation key")} />
          </span>
        </Fact>
      )}
      {item.due_at && !item.scheduled && (
        <Fact label={t_i18n("Due")} full={formatShortDateTime(item.due_at)}>
          <span className="inline-flex min-w-0 items-center gap-1">
            <CalendarClock className="h-3 w-3 shrink-0" /> <span className="truncate">{formatShortDateTime(item.due_at)}</span>
          </span>
        </Fact>
      )}
      {item.created_at && (
        <Fact label={t_i18n("Created")} full={formatShortDateTime(item.created_at)}>
          <span className="truncate">{formatShortDateTime(item.created_at)}</span>
        </Fact>
      )}
      {item.last_activity_at && (
        <Fact label={t_i18n("Last activity")} full={formatShortDateTime(item.last_activity_at)}>
          <span className="truncate">{formatTimeAgo(intl, item.last_activity_at)}</span>
        </Fact>
      )}
    </dl>
  );
}

function Timeline({ item }: { item: WorkItem }) {
  const { t_i18n, intl } = useFormatter();
  const rows: [string, string | null][] = [
    [t_i18n("Created"), item.created_at],
    [t_i18n("Due"), item.scheduled ? null : item.due_at],
    [t_i18n("Claimed"), item.claimed_at],
    [t_i18n("Last activity"), item.last_activity_at],
    [t_i18n("Updated"), item.updated_at],
  ];
  return (
    <Section icon={Clock} title={t_i18n("Timeline")}>
      <div className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-3">
        {rows
          .filter((row): row is [string, string] => !!row[1])
          .map(([label, at]) => (
            <div key={label}>
              <div className="content-caption text-default-secondary">{label}</div>
              <div>{formatShortDateTimeIntl(intl, at)}</div>
            </div>
          ))}
      </div>
    </Section>
  );
}

function Body({ item, actions, onClose }: { item: WorkItem; actions: WorkItemActions; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const context = item.context;
  const scheduled = isScheduledItem(item) ? item : null;
  const payload = visiblePayload(item.payload);
  const closable = canClose(item) && !!actions.close;
  // Read but not writable for the reader (#1845): the write actions stay
  // visible, disabled, with why, instead of answering "not found".
  const lockedReason = item.can_write === false ? item.read_only_reason || t_i18n("Read-only for you.") : null;
  // The decision recipient may cancel an item they cannot otherwise change (#1910).
  const cancelLocked = item.can_cancel === true ? null : lockedReason;
  // An item waiting for the reader leads with its decision; the run notes
  // below it are history, folded until asked for.
  const deciding = !scheduled && waitsForAnswer(item);
  // A bare #N and a commit SHA link to the one repository the item's PRs live in.
  const repos = [...new Set(pullRequestsOf(item.payload).map((pr) => pr.repo))];
  const description = item.description ? (
    <WorkItemActivity description={item.description} runs={item.note_runs} repo={repos.length === 1 ? repos[0] : null} />
  ) : (
    <p className="text-default-secondary">{t_i18n("No description.")}</p>
  );
  return (
    <>
      <DialogHeader>
        <div className="flex flex-wrap items-center gap-1.5">
          {scheduled ? (
            <ScheduleStateBadge scheduled={scheduled.scheduled} />
          ) : blockedKind(item) ? (
            // The same chip as the row: Waiting or Blocked, with why.
            <BlockedStatusBadge
              kind={blockedKind(item) ?? "action"}
              blockers={unfinishedBlockers(item).map((b) => b.title || t_i18n("a work item you cannot open"))}
              cancelled={cancelledBlockers(item).map((b) => b.title || t_i18n("a work item you cannot open"))}
              reason={blockedReason(item)}
            />
          ) : (
            <Badge variant="secondary" className="content-caption">{t_i18n(STATUS_LABELS[item.status])}</Badge>
          )}
          {!scheduled && blockedWithoutReason(item) && <WorkItemNoReasonChip />}
          <Badge variant="secondary" className="content-caption">{t_i18n(PRIORITY_LABELS[item.priority])}</Badge>
          {item.is_stale && (
            <Tip content={staleReasonText(t_i18n, item)} wrap>
              <Badge
                variant="secondary"
                className="content-caption gap-1 text-feedback-warning-tertiary bg-feedback-alert-primary/10"
                data-testid="work-item-stale-badge"
              >
                <AlertTriangle className="h-3 w-3" aria-hidden /> {staleBadgeText(t_i18n, item)}
              </Badge>
            </Tip>
          )}
          {context && <WorkItemVisibilityBadge visibility={context.visibility} />}
        </div>
      </DialogHeader>

      <div className="max-h-[62vh] space-y-5 overflow-y-auto pr-1" data-testid="work-item-detail">
        {deciding && <WorkItemDecisionCard item={item} canAnswer={!!actions.answer} onAnswered={onClose} />}
        {/* An item on a CTEM case, answered or not, opens its case first (#2370); the card above carries it when deciding. */}
        {!deciding && <WorkItemCaseLink item={item} />}
        {!deciding && !scheduled && offersAskTheAgent(item) && (
          <section className="rounded-sm border border-elevation-subtle p-3" data-testid="work-item-ask-agent-section">
            <WorkItemAskAgent item={item} bordered={false} onDone={onClose} />
          </section>
        )}
        <Facts item={item} context={context} />
        {scheduled && <ScheduledDetails item={scheduled} full />}
        <Section icon={FileText} title={t_i18n("Details")}>
          {deciding && item.description ? (
            <details data-testid="work-item-description-folded">
              <DisclosureSummary>{t_i18n("Show the full description")}</DisclosureSummary>
              <div className="pt-2">{description}</div>
            </details>
          ) : (
            description
          )}
        </Section>
        {item.result && !scheduled && (
          <Section icon={CheckCircle2} title={t_i18n("Result")}>
            <ChatMarkdown content={item.result} className="chat-markdown content-base" remoteImages={false} />
            <OutcomeReview block={item.roi} className="mt-3" />
          </Section>
        )}
        {(item.attachments?.files.length || item.attachments?.gone) ? (
          <Section icon={Paperclip} title={t_i18n("Files")}>
            <WorkItemFiles attachments={item.attachments} />
          </Section>
        ) : null}
        {context && !scheduled && <WhoActs item={item} context={context} />}
        {context && (
          <Section icon={Eye} title={t_i18n("Why you see it")}>
            <p>
              {t_i18n(VISIBILITY_EXPLANATIONS[context.visibility.reason], {
                workspace: context.visibility.workspace?.name ?? "",
              })}
            </p>
          </Section>
        )}
        {payload && (
          <Section icon={Braces} title={t_i18n("Payload")}>
            <pre className="max-h-56 overflow-auto rounded-sm border border-elevation-subtle bg-elevation-hover p-2 font-mono-plex text-[11px]">
              {JSON.stringify(payload, null, 2)}
            </pre>
          </Section>
        )}
        <Timeline item={item} />
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2 border-t border-elevation-subtle pt-3">
        {actions.remove && (
          <Tip content={lockedReason} wrap="auto" wrapClassName="mr-auto inline-flex">
            <Button
              variant="ghost"
              size="sm"
              className="mr-auto gap-1 text-feedback-error-primary"
              disabled={actions.busy || !!lockedReason}
              onClick={() => actions.remove?.(item)}
            >
              <Trash2 className="h-3.5 w-3.5" /> {t_i18n("Delete")}
            </Button>
          </Tip>
        )}
        {scheduled && <ScheduleActions item={scheduled} />}
        {actions.requeue && canRequeue(item) && (
          <Tip content={lockedReason ?? t_i18n(REQUEUE_EFFECT)} wrap="auto">
            <Button
              variant="ghost"
              size="sm"
              className="gap-1"
              disabled={actions.busy || !!lockedReason}
              onClick={() => actions.requeue?.(item)}
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Put back in the queue")}
            </Button>
          </Tip>
        )}
        {closable && (
          <>
            <Tip content={cancelLocked ?? t_i18n(CLOSE_EFFECTS.cancelled)} wrap="auto">
              <Button
                variant="ghost"
                size="sm"
                className="gap-1"
                disabled={!!cancelLocked}
                onClick={() => actions.close?.(item, "cancelled")}
              >
                <XCircle className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Cancel work item")}
              </Button>
            </Tip>
            {/* Not on an item that waits for open dependencies: the row hides it too. */}
            {offersMarkDone(item) && (
              <Tip content={lockedReason ?? t_i18n(CLOSE_EFFECTS.done)} wrap="auto">
                <Button size="sm" className="gap-1" disabled={!!lockedReason} onClick={() => actions.close?.(item, "done")}>
                  <CheckCircle2 className="h-3.5 w-3.5" aria-hidden /> {t_i18n("Mark done")}
                </Button>
              </Tip>
            )}
          </>
        )}
      </div>
    </>
  );
}

export function WorkItemDetailDialog({
  itemId,
  onClose,
  actionsFor,
}: {
  itemId: string | null;
  onClose: () => void;
  /** The actions the reader may take on this item. */
  actionsFor: (item: WorkItem) => WorkItemActions;
}) {
  const { t_i18n } = useFormatter();
  const { data, isLoading, isError } = useWorkItem(itemId);
  // A failed refetch keeps the last data: an item deleted from here is gone.
  const item = isError ? undefined : data;
  return (
    <Dialog open={itemId !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogTitle className={item && !isLoading ? "pr-8 text-base leading-snug" : "sr-only"}>
          {isLoading ? t_i18n("Loading the work item") : item ? item.title : t_i18n("Work item")}
        </DialogTitle>
        <DialogDescription className="sr-only">
          {t_i18n("Who acts on this work item, where it came from and why you see it.")}
        </DialogDescription>
        {isLoading ? (
          <div className="space-y-3" aria-busy="true">
            <Spinner size="sm" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
            <Skeleton className="h-6 w-2/3" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : item ? (
          <Body item={item} actions={actionsFor(item)} onClose={onClose} />
        ) : (
          <EmptyState
            icon={ListTodo}
            title="This work item is no longer available."
            description="It was deleted, or it is not one you can see."
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

