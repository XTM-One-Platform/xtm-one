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
 * The pieces every work-item view draws from an item's `context`: an
 * agent (by name and linked, or as one the reader cannot open - never a
 * placeholder), the pickup state, why the reader sees it and where it came
 * from. The rows of Operations > Work Items and the detail dialog share them.
 */
import { workItemPath } from "@/lib/task-routes";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ElementType, MouseEvent, ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  Ban,
  Bot,
  Building2,
  CalendarClock,
  CheckCircle2,
  CircleDashed,
  CircleDot,
  FolderKanban,
  GitMerge,
  GitPullRequest,
  GitPullRequestClosed,
  Hand,
  History,
  Link2,
  Lock,
  MessageSquare,
  User,
  Workflow,
  Zap,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { listOverflows } from "@/components/ui/detail-tabs-fit";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useFormatter } from "@/i18n";
import { ICON_MAP } from "@/lib/constants";
import { formatList } from "@/lib/format-list";
import { cn } from "@/lib/utils";
import { endedLabel } from "@/lib/work-item-holding-run";
import {
  BADGE_TIP_FOCUS,
  blockedRefLabel,
  PICKUP_LABELS,
  PICKUP_TONE,
  VISIBILITY_LABELS,
  pullRequestStateOf,
  type WorkItemPullRequest,
} from "@/lib/work-item-context";
import type {
  WorkItemAgentRef,
  WorkItemBlockedRef,
  WorkItemBlockerRef,
  WorkItemContext,
  WorkItemFleetImpactRef,
  WorkItemHoldingRun,
  WorkItemPickupState,
} from "@/types";

const stop = (e: MouseEvent) => e.stopPropagation();

const PICKUP_ICONS: Record<WorkItemPickupState, ElementType> = {
  scheduled: CalendarClock,
  finished: CheckCircle2,
  in_progress: CircleDashed,
  blocked: Ban,
  blocked_on_items: Link2,
  waiting_for_refs: Link2,
  waiting_for_person: User,
  waiting_for_ci: GitPullRequest,
  unassigned: AlertTriangle,
  automatic: Zap,
  waiting_until_due: CalendarClock,
  filtered_out: AlertTriangle,
  manual: Hand,
};

/** An agent named on an item: linked by name, or one the reader cannot open. */
export function WorkItemAgent({ agent, className }: { agent: WorkItemAgentRef; className?: string }) {
  const { t_i18n } = useFormatter();
  if (!agent.accessible || !agent.id) {
    const why = t_i18n("This agent is not shared with you: you can see the work item, not the agent.");
    // The label is cut in a narrow column: the tooltip names the agent in full
    // on its first line, then says why it cannot be opened.
    return (
      <Tip
        content={
          agent.name ? (
            <span className="flex flex-col gap-0.5">
              <span className="font-medium">{agent.name}</span>
              <span>{why}</span>
            </span>
          ) : (
            why
          )
        }
        wrap
        wrapClassName="inline-flex min-w-0 max-w-full"
      >
        <span className={cn("inline-flex min-w-0 items-center gap-1 italic text-default-secondary", className)}>
          <Lock className="h-3 w-3 shrink-0" />
          <span className="truncate" data-testid="work-item-agent-hidden-label">
            {agent.name ?? t_i18n("an agent you cannot open")}
          </span>
        </span>
      </Tip>
    );
  }
  const Icon = ICON_MAP[agent.icon ?? ""] ?? Bot;
  // `max-w-full` keeps the link inside its box (a table cell, a token), so the name ends
  // with an ellipsis instead of growing past it; the shared tooltip names it whole when cut.
  return (
    <Link
      to={`/agents/${agent.id}`}
      onClick={stop}
      data-testid="work-item-agent-link"
      className={cn("inline-flex min-w-0 max-w-full items-center gap-1 text-default-primary hover:text-accent hover:underline", className)}
    >
      <Icon className="h-3 w-3 shrink-0" />
      <TruncatedText text={agent.name ?? ""} />
    </Link>
  );
}

/**
 * The run holding the item's claim: "run 1a2b3c4d, alive" or "run
 * 1a2b3c4d, ended (failed)", the run linked when it is an agent run.
 */
export function WorkItemHoldingRunLink({ run }: { run: WorkItemHoldingRun }) {
  const { t_i18n } = useFormatter();
  const short = run.id.slice(0, 8);
  const label = t_i18n("run {id}", { id: short });
  const name =
    run.kind === "run" ? (
      <Link to={`/operations/executions/${run.id}`} onClick={stop} className="inline-flex items-center gap-1 font-mono text-accent hover:underline">
        <History className="h-3 w-3 shrink-0" />
        {label}
      </Link>
    ) : (
      <span className="inline-flex items-center gap-1 font-mono">
        <History className="h-3 w-3 shrink-0" />
        {label}
      </span>
    );
  return (
    <span className="inline-flex min-w-0 items-center whitespace-nowrap" data-testid="work-item-holding-run">
      {name}
      <span className="text-default-secondary">,</span>
      <span className={cn("ms-1", run.alive ? "text-feedback-success-primary" : "text-feedback-warning-primary")}>
        {run.alive ? t_i18n("alive") : t_i18n(endedLabel(run.status))}
      </span>
    </span>
  );
}

type Translate = (id: string, values?: Record<string, string | number>) => string;

interface PickupWording {
  state: WorkItemPickupState;
  blockedOn?: number;
  waiting?: boolean;
  pullRequest?: { number: number } | null;
  pullRequests?: { repo: string; number: number }[] | null;
}

/**
 * The words of the pickup badge: its `label` and the `tip` that may add to it
 * (every pull request it waits for). The badge and the row's hover card read
 * the same words (the hover card names what a narrow row cannot show).
 */
export function pickupWording(
  t_i18n: Translate,
  locale: string,
  { state, blockedOn, waiting = false, pullRequest, pullRequests }: PickupWording,
): { label: string; tip: string } {
  let label = t_i18n(PICKUP_LABELS[state]);
  let tip = label;
  if (state === "blocked_on_items" && blockedOn) {
    label = waiting
      ? t_i18n("Waits for {count, plural, one {# item} other {# items}}", { count: blockedOn })
      : t_i18n("Blocked on {count, plural, one {# item} other {# items}}", { count: blockedOn });
    tip = label;
  } else if (state === "waiting_for_ci") {
    const prs = pullRequests?.length ? pullRequests : pullRequest ? [pullRequest] : [];
    if (prs.length > 1) {
      label = t_i18n("Waiting for CI on #{number} and {count} more", { number: prs[0].number, count: prs.length - 1 });
      tip = t_i18n("Waiting for CI on {list}", {
        list: formatList(
          prs.map((pr) => ("repo" in pr ? `${pr.repo}#${pr.number}` : `#${pr.number}`)),
          locale,
        ),
      });
    } else if (prs.length === 1) {
      label = t_i18n("Waiting for CI on #{number}", { number: prs[0].number });
      tip = label;
    }
  }
  return { label, tip };
}

/**
 * What will act on the item, as a badge. An item blocked on other items
 * reads "Blocked on N item(s)" when *blockedOn* is given, or "Waits
 * for N item(s)" when *waiting* says every one of them is still open and it
 * goes back to the queue by itself (one word for one state, as the
 * row's "Waiting" chip); one waiting
 * on CI names its pull request when *pullRequest* is given, and how
 * many more wait with it when *pullRequests* lists them all: the
 * tooltip then names every one.
 */
export function WorkItemPickupBadge({
  state,
  blockedOn,
  waiting = false,
  pullRequest,
  pullRequests,
  compact = false,
}: {
  state: WorkItemPickupState;
  blockedOn?: number;
  /** The item waits for open dependencies only (`blockedKind(item) === "waiting"`). */
  waiting?: boolean;
  pullRequest?: { number: number } | null;
  pullRequests?: { repo: string; number: number }[] | null;
  /** The icon alone, its label in a tooltip (a fixed-height list row). */
  compact?: boolean;
}) {
  const { t_i18n, locale } = useFormatter();
  const Icon = PICKUP_ICONS[state];
  const { label, tip } = pickupWording(t_i18n, locale, { state, blockedOn, waiting, pullRequest, pullRequests });
  if (compact) {
    return (
      <Tip content={tip}>
        <Badge
          variant="secondary"
          role="img"
          aria-label={label}
          tabIndex={0}
          className={cn("content-compact px-1.5", BADGE_TIP_FOCUS, PICKUP_TONE[state])}
          data-testid="work-item-pickup"
        >
          <Icon className="h-3 w-3" />
        </Badge>
      </Tip>
    );
  }
  if (tip === label) {
    return (
      <Badge variant="secondary" className={cn("content-compact gap-1", PICKUP_TONE[state])} data-testid="work-item-pickup">
        <Icon className="h-3 w-3" /> {label}
      </Badge>
    );
  }
  // A tooltip that adds to the label is a tab stop, as the compact badge is.
  return (
    <Tip content={tip}>
      <Badge
        variant="secondary"
        tabIndex={0}
        className={cn("content-compact gap-1", BADGE_TIP_FOCUS, PICKUP_TONE[state])}
        data-testid="work-item-pickup"
      >
        <Icon className="h-3 w-3" /> {label}
      </Badge>
    </Tip>
  );
}

/** `owner/name#N` as `name#N`: the repository name and the number, for a narrow row. */
function shortPullRequestRef(pr: { repo: string; number: number }): string {
  const name = pr.repo.split("/").pop() || pr.repo;
  return `${name}#${pr.number}`;
}

const PULL_REQUEST_STATE_ICONS = { open: GitPullRequest, merged: GitMerge, closed: GitPullRequestClosed } as const;

/** The icon of a reference a blocked item waits for, by kind. */
const REF_ICONS: Record<WorkItemBlockedRef["kind"], ElementType> = {
  pull_request: GitPullRequest,
  issue: CircleDot,
  deployment: GitMerge,
  workflow_run: Workflow,
};
/** The kind of a reference, the prefix of its accessible name. Read by `t_i18n(REF_KIND_LABELS[kind])`. @i18n-keys */
const REF_KIND_LABELS = {
  pull_request: "Pull request",
  issue: "Issue",
  deployment: "Deployment",
  workflow_run: "Workflow run",
} as const satisfies Record<WorkItemBlockedRef["kind"], string>;
/** Read by `t_i18n(PULL_REQUEST_STATE_TIPS[state])`. @i18n-keys */
const PULL_REQUEST_STATE_TIPS = {
  open: "Open pull request",
  merged: "Merged pull request",
  closed: "Closed pull request",
} as const;

/**
 * The items an item waits for: each linked by its title, or unnamed
 * when the reader cannot open it. `nowrap` keeps them on one line, the last
 * one truncated (a fixed-height list row); the dialog lets them wrap.
 */
export function WorkItemBlockers({ blockers, nowrap = false }: { blockers: WorkItemBlockerRef[]; nowrap?: boolean }) {
  const { t_i18n } = useFormatter();
  return (
    <span
      className={cn("inline-flex min-w-0 max-w-full items-center gap-x-2", nowrap ? "flex-nowrap overflow-hidden" : "flex-wrap")}
      data-testid="work-item-blockers"
    >
      {blockers.map((blocker, index) =>
        blocker.accessible && blocker.id ? (
          <Link
            key={blocker.id}
            to={workItemPath(blocker.id)}
            onClick={stop}
            className="inline-flex min-w-0 max-w-full items-center gap-1 text-accent hover:underline"
          >
            <Link2 className="h-3 w-3 shrink-0" />
            <span className="truncate">{blocker.title || t_i18n("Untitled work item")}</span>
          </Link>
        ) : (
          <span key={`hidden-${index}`} className="inline-flex items-center gap-1 italic text-default-secondary">
            <Lock className="h-3 w-3 shrink-0" />
            {t_i18n("a work item you cannot open")}
          </span>
        ),
      )}
    </span>
  );
}

/**
 * What a blocked item waits for besides other items: the pull
 * requests and issues still open (`blocked_on_refs`), each with its kind by
 * icon and by accessible name, the reason written on it and, when it waits
 * for a person's decision, that person. Plain text: the reference names a
 * repository of whichever code host, so the view does not guess a link.
 * `nowrap` keeps it on one line, as `WorkItemBlockers` does: there a
 * reference is never narrower than its icon and `#N` (only the repository
 * part truncates), and the references that do not fit whole give way to a
 * "+N" naming the rest; the row's tooltip and the dialog list
 * them all. When the line still overflows with every reference folded, the
 * person part gives way too, to its icon alone named by its accessible name
 * and title, so the reason keeps its floor inside the line.
 */
export function WorkItemBlockedWhy({
  refs,
  reason,
  needsHuman = false,
  nowrap = false,
}: {
  refs: WorkItemBlockedRef[];
  reason?: string | null;
  needsHuman?: boolean;
  nowrap?: boolean;
}) {
  const { t_i18n, locale } = useFormatter();
  const text = reason?.trim();
  const boxRef = useRef<HTMLSpanElement>(null);
  // The width the line may take: its parent's, which never depends on how
  // many references are shown, so fitting them cannot loop.
  const [roomWidth, setRoomWidth] = useState(0);
  // How many parts gave way: the first `refs.length` steps fold one reference
  // each into "+N", the next one shows the person part as its icon alone.
  const [fit, setFit] = useState<{ key: string; step: number } | null>(null);
  const measureKey = `${roomWidth}|${refs.map(blockedRefLabel).join("|")}|${text ?? ""}|${needsHuman}`;
  const step = nowrap && fit?.key === measureKey ? fit.step : 0;
  const lastStep = refs.length + (needsHuman ? 1 : 0);
  const count = Math.max(0, refs.length - step);
  const personCompact = needsHuman && step > refs.length;
  useEffect(() => {
    const room = boxRef.current?.parentElement;
    if (!nowrap || !room || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setRoomWidth(room.clientWidth));
    observer.observe(room);
    return () => observer.disconnect();
  }, [nowrap]);
  // Before paint, while the line still overflows, drop the last reference into
  // "+N", then shorten the person part; a new width or new content measures
  // again from the full line.
  useLayoutEffect(() => {
    const box = boxRef.current;
    if (!nowrap || !box || step >= lastStep) return;
    if (listOverflows({ scrollWidth: box.scrollWidth, clientWidth: box.clientWidth })) {
      setFit({ key: measureKey, step: step + 1 });
    }
  }, [nowrap, step, lastStep, measureKey]);
  if (!refs.length && !text && !needsHuman) return null;
  const hidden = refs.slice(count);
  return (
    <span
      ref={boxRef}
      className={cn("inline-flex min-w-0 max-w-full items-center gap-x-2", nowrap ? "flex-nowrap overflow-hidden" : "flex-wrap")}
      data-testid="work-item-blocked-why"
    >
      {refs.slice(0, count).map((ref) => {
        const label = blockedRefLabel(ref);
        // On one line the repository shows as `name` (as WorkItemPullRequestChips
        // does) and only it truncates: `#N`, the part that tells the references
        // apart, always stays whole.
        const repoText = nowrap ? ref.repo.split("/").pop() || ref.repo : ref.repo;
        const Icon = REF_ICONS[ref.kind] ?? GitPullRequest;
        const kind = t_i18n(REF_KIND_LABELS[ref.kind] ?? REF_KIND_LABELS.pull_request);
        // The part that tells references apart, never truncated: #N, @sha or the workflow.
        const tail =
          ref.kind === "workflow_run"
            ? `: ${ref.workflow ?? ""}`
            : ref.number != null
              ? `#${ref.number}`
              : ref.sha
                ? `@${ref.sha.slice(0, 7)}`
                : "";
        return (
          <span
            key={`${ref.kind}:${label}`}
            role="img"
            aria-label={`${kind}: ${label}`}
            title={label}
            data-blocked-ref={label}
            className="inline-flex shrink items-center gap-1 text-default-primary"
          >
            {/* No `min-w-0` here nor on the wrapper: their floor is the icon,
                the gap and `#N`, so only the `truncate` repository part
                shrinks and a reference is never cut through its number. */}
            <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
            <span className="inline-flex items-center">
              <span className="truncate">{repoText}</span>
              <span className="shrink-0">{tail}</span>
            </span>
          </span>
        );
      })}
      {hidden.length > 0 && (
        <span
          role="img"
          aria-label={t_i18n("{count, plural, one {# more pull request or issue} other {# more pull requests or issues}}", {
            count: hidden.length,
          })}
          title={formatList(hidden.map(blockedRefLabel), locale)}
          className="shrink-0 text-default-secondary"
          data-testid="work-item-blocked-refs-more"
        >
          +{hidden.length}
        </span>
      )}
      {needsHuman &&
        (personCompact ? (
          <span
            role="img"
            aria-label={t_i18n("a person's decision")}
            title={t_i18n("a person's decision")}
            className="inline-flex shrink-0 items-center text-default-primary"
            data-testid="work-item-blocked-person"
            data-compact="true"
          >
            <User className="h-3 w-3 shrink-0" aria-hidden="true" />
          </span>
        ) : (
          <span
            className="inline-flex shrink-0 items-center gap-1 text-default-primary"
            data-testid="work-item-blocked-person"
            data-compact="false"
          >
            <User className="h-3 w-3 shrink-0" aria-hidden="true" />
            {t_i18n("a person's decision")}
          </span>
        ))}
      {/* On one line the reason gives way first, down to a minimum share so it
          never renders 0 px wide; the references shrink only
          after it, and never past their `#N`. */}
      {text && (
        <span
          data-testid="work-item-blocked-reason"
          title={nowrap ? text : undefined}
          className={cn(nowrap ? "min-w-[6rem] shrink-[1000] truncate" : "min-w-0 break-words")}
        >
          {text}
        </span>
      )}
    </span>
  );
}

/**
 * The red chip of a blocked item that says nothing of what it waits for:
 * a legacy item, blocked before the platform asked why. Its tooltip
 * says what to set; it goes once the item is edited.
 */
export function WorkItemNoReasonChip() {
  const { t_i18n } = useFormatter();
  return (
    <Tip
      content={t_i18n(
        "Blocked with no reason: nothing says what it waits for. Set the items, pull requests or issues it waits for, a reason, or that it needs a person.",
      )}
      wrap
    >
      <Badge
        variant="secondary"
        tabIndex={0}
        className={cn("content-compact gap-1", BADGE_TIP_FOCUS, "text-feedback-error-primary bg-feedback-error-primary/10")}
        data-testid="work-item-no-reason"
      >
        <AlertTriangle className="h-3 w-3" aria-hidden="true" />
        {t_i18n("No reason")}
      </Badge>
    </Tip>
  );
}

/**
 * The pull requests an item names (`payload.pull_requests`), linked to their
 * page. Each says its state by icon and tooltip, not by colour alone.
 * `nowrap` keeps them on one line, as `WorkItemBlockers` does; a row that
 * already puts one tooltip on the whole strip passes `tips={false}`. `short`
 * shows `name#N` so a narrow row keeps the number visible; the accessible
 * name and the tooltip keep `owner/name#N`. `max` shows that many chips in
 * full and a `+N` chip for the rest, so no number is ever cut; the
 * row's tooltip names every one.
 */
export function WorkItemPullRequestChips({
  pullRequests,
  nowrap = false,
  tips = true,
  short = false,
  max,
}: {
  pullRequests: WorkItemPullRequest[];
  nowrap?: boolean;
  tips?: boolean;
  short?: boolean;
  max?: number;
}) {
  const { t_i18n } = useFormatter();
  if (pullRequests.length === 0) return null;
  const shown = max && pullRequests.length > max ? pullRequests.slice(0, max) : pullRequests;
  const hidden = pullRequests.length - shown.length;
  return (
    <span
      className={cn("inline-flex min-w-0 max-w-full items-center gap-1", nowrap ? "flex-nowrap overflow-hidden" : "flex-wrap")}
      data-testid="work-item-pull-requests"
    >
      {shown.map((pr) => {
        const text = `${pr.repo}#${pr.number}`;
        const state = pullRequestStateOf(pr.state);
        const Icon = PULL_REQUEST_STATE_ICONS[state];
        const stateLabel = t_i18n(PULL_REQUEST_STATE_TIPS[state]);
        const tone = state === "open" ? "text-accent" : "text-default-secondary";
        const body = (
          <>
            <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
            <span className="truncate">{short ? shortPullRequestRef(pr) : text}</span>
          </>
        );
        const chip = pr.url ? (
          <a
            href={pr.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={stop}
            aria-label={`${stateLabel}: ${text}`}
            data-state={state}
            className={cn("inline-flex min-w-0 items-center gap-1 hover:underline", tone)}
          >
            {body}
          </a>
        ) : (
          <span
            role="img"
            aria-label={`${stateLabel}: ${text}`}
            data-state={state}
            className={cn("inline-flex min-w-0 items-center gap-1", tone)}
          >
            {body}
          </span>
        );
        return tips ? (
          <Tip key={text} content={`${stateLabel}: ${text}`}>
            {chip}
          </Tip>
        ) : (
          <span key={text} className="inline-flex min-w-0">
            {chip}
          </span>
        );
      })}
      {hidden > 0 && (
        <span
          role="img"
          aria-label={t_i18n("{count, plural, one {# more pull request} other {# more pull requests}}", { count: hidden })}
          className="shrink-0 text-default-secondary"
          data-testid="work-item-pull-requests-more"
        >
          +{hidden}
        </span>
      )}
    </span>
  );
}

/**
 * The agents an item's completion is routed to, each linked by name or
 * shown as one the reader cannot open. `nowrap` keeps them on one line, the
 * last one truncated (a fixed-height list row); the dialog lets them wrap.
 */
export function WorkItemImpacts({ impact, nowrap = false }: { impact: WorkItemFleetImpactRef; nowrap?: boolean }) {
  return (
    <span
      className={cn("inline-flex min-w-0 max-w-full items-center gap-x-2", nowrap ? "flex-nowrap overflow-hidden" : "flex-wrap")}
      data-testid="work-item-impacts"
    >
      {impact.agents.map((agent, index) => (
        <WorkItemAgent
          key={agent?.id ?? `hidden-${index}`}
          agent={agent ?? { id: null, name: null, icon: null, accessible: false }}
        />
      ))}
    </span>
  );
}

/** Why the reader sees the item, when it is not simply theirs. */
export function WorkItemVisibilityBadge({ visibility }: { visibility: WorkItemContext["visibility"] }) {
  const { t_i18n } = useFormatter();
  if (visibility.reason === "own") return null;
  const workspace = visibility.reason === "workspace";
  return (
    <Badge variant="outline" className="content-compact gap-1">
      {workspace ? <FolderKanban className="h-3 w-3" /> : <Building2 className="h-3 w-3" />}
      {workspace && visibility.workspace?.name ? visibility.workspace.name : t_i18n(VISIBILITY_LABELS[visibility.reason])}
    </Badge>
  );
}

/** The chat, run or flow run the item was created in, linked when the reader may open it. */
export function WorkItemOriginLink({ origin }: { origin: WorkItemContext["origin"] }) {
  const { t_i18n } = useFormatter();
  const linkClass = "inline-flex min-w-0 items-center gap-1 text-accent hover:underline";
  const hidden = (icon: ReactNode, label: string) => (
    <span className="inline-flex min-w-0 items-center gap-1 italic text-default-secondary">
      {icon}
      <span className="truncate">{label}</span>
    </span>
  );
  if (origin.flow_run) {
    const run = origin.flow_run;
    if (!run.accessible || !run.id || !run.flow_id) {
      return hidden(<Workflow className="h-3 w-3" />, t_i18n("a flow run you cannot open"));
    }
    return (
      <Link to={`/flows/library/${run.flow_id}?run=${run.id}`} onClick={stop} className={linkClass}>
        <Workflow className="h-3 w-3 shrink-0" />
        <span className="truncate">{run.name ?? t_i18n("Flow run")}</span>
      </Link>
    );
  }
  if (origin.run) {
    const run = origin.run;
    if (!run.accessible || !run.id) return hidden(<History className="h-3 w-3" />, t_i18n("a run you cannot open"));
    return (
      <Link to={`/operations/executions/${run.id}`} onClick={stop} className={linkClass}>
        <History className="h-3 w-3 shrink-0" />
        <span className="truncate">{run.name ?? t_i18n("Agent run")}</span>
      </Link>
    );
  }
  if (origin.conversation) {
    return (
      <Link to={`/chat?c=${origin.conversation.id}`} onClick={stop} className={linkClass}>
        <MessageSquare className="h-3 w-3 shrink-0" />
        <span className="truncate">{origin.conversation.title || t_i18n("A chat")}</span>
      </Link>
    );
  }
  if (origin.conversation_hidden) {
    return hidden(<MessageSquare className="h-3 w-3" />, t_i18n("another person's chat"));
  }
  return null;
}

/** The person the item acts for: you, or a colleague by name. */
export function WorkItemPerson({ person }: { person: NonNullable<WorkItemContext["person"]> }) {
  const { t_i18n } = useFormatter();
  return (
    <span className="inline-flex min-w-0 items-center gap-1">
      <User className="h-3 w-3 shrink-0" />
      <span className="truncate">{person.is_you ? t_i18n("you") : person.name || t_i18n("a deleted account")}</span>
    </span>
  );
}
