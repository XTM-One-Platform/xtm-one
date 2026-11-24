// SPDX-License-Identifier: Apache-2.0

/**
 * The Act now queue of the CTEM command center (C2 slice V2, refocused by
 * #10721): what to do first, at most five rows. Each row is one line, the
 * whole row opens its target, the first one is marked "Do first"; the source
 * and the priority are a small label, never a button column. The rows come
 * from the posture (module facts, one row per stage, blind spots, setup) and
 * from what waits for the reader (work items, tool approvals). The briefing's
 * next steps are in the Exposure briefing widget and the stage assessments on
 * the stage pages: the queue never repeats them. Nothing here starts a run.
 *
 * #11213: each row shows its one-line why; a fact or a setup row carries Ask
 * AI, which opens the chat with the row's context in the composer (never sent
 * on its own); when the module facts are not tailored to the organization the
 * card says so, with the way to the organization context; no "All work" link,
 * since no page lists these rows.
 *
 * #11547 slice S2d (design note dev-docs/ctem-stage-viz.md section 4, row
 * act_now): a segmented bar of what waits, by source (posture, blind spots,
 * setup, waiting for you), its legend a swatch and a count before each label;
 * then three rows by default, each led by a dot in its source's colour.
 */
import { waitingForMePath } from "@/lib/task-routes";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronRight, Inbox, ShieldQuestion, Zap } from "lucide-react";
import { Chip, Icon, Spinner } from "@filigran/design-system";

import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { Tip } from "@/components/ui/tooltip";
import { ToneBadge } from "@/components/ui/tone-badge";
import { primeChatDraft } from "@/lib/chat-launch";
import { useFormatter } from "@/i18n";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { cn, formatCompactNumber } from "@/lib/utils";
import { CTEM_PRIORITY_CONFIG } from "@/lib/constants";
import { useWorkItems, useWorkItemsWaitingForMe } from "@/hooks/api/useWorkItemApi";
import { waitsForAnswer } from "@/lib/work-item-context";
import { REMEDIATION_DECISION_PREFIX, caseIdOfDecisionKey } from "./case-gates";
import { usePendingApprovalsByTool } from "@/hooks/api/useAssignmentApi";
import { useCtemCases } from "@/hooks/api/useCtemCasesApi";
import { useCtemSignalList } from "@/hooks/api/useCtemSignalsApi";
import { CRITICALITY_LABELS } from "@/lib/organization-profile";
import { useNow } from "@/hooks/useTick";
import { CaseUrgencyChip, slaCountdown, slaSentence } from "./case-loop";
import type { AiLockReason } from "@/hooks/useAiLockReason";
import { GenericContextNotice } from "./GenericContextNotice";
import { RecommendedActionsEmpty } from "./RecommendedActionsEmpty";
import {
  ACT_NOW_COLLAPSED, ACT_NOW_GROUP_FILLS, ACT_NOW_GROUP_LABELS, ACT_NOW_PRIORITY, actNowAskAiPrompt, actNowGroup, actNowGroupCounts,
  actNowCasePriority, actNowCases, actNowIsGeneric, actNowOffersAskAi, actNowPriorityLabel, actNowQueue, actNowShowsPriority, caseScopes,
  type ActNowRow,
} from "./act-now";

export interface ActNowCardProps {
  /** The rows the page builds: blind spots, module facts and setup. */
  rows: ActNowRow[];
  /** The briefing's next steps, shown in the briefing widget: a row saying the same is left out. */
  briefingActions?: string[];
  aiLockReason: AiLockReason | null;
  /** The height in pixels its content needs, so the command center sizes the
   *  card to its rows: no empty band, no inner scroll on Show all (UX round 1
   *  on #10357, B1 and B2). */
  onContentHeight?: (px: number) => void;
  /** The posture or a stage assessment behind the rows is being rebuilt: the
   *  header spins and says so until the fresh rows land (#12183). */
  refreshing?: boolean;
}

/** Whether a one-line text node is cut by its box, kept current on resize.
 *  The ref is a callback ref held in state: when the title starts to cut, the
 *  row gains its tooltip wrapper and the span is mounted again, so the effect
 *  must observe the new span, never the detached one, which measures 0 and
 *  would read the title as fitting again (UX round 2 on #10723, B1). */
function useIsCut(text: string) {
  const [node, ref] = useState<HTMLSpanElement | null>(null);
  const [cut, setCut] = useState(false);
  useLayoutEffect(() => {
    if (!node) return;
    const measure = () => setCut(node.scrollWidth > node.clientWidth + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [node, text]);
  return { ref, cut };
}

/** One row of the queue: the whole row is the button, and when the line cuts
 *  the title the full text is a tooltip on that button, so it opens on hover
 *  and on keyboard focus alike (UX round 1 on #10723, B1). The why sits under
 *  the title; Ask AI is a sibling button, never nested in the row button. */
function ActNowRowButton({
  row, first, onAskAi, askAiSlot,
}: { row: ActNowRow; first: boolean; onAskAi?: () => void; askAiSlot: boolean }) {
  const { t_i18n } = useFormatter();
  const { ref, cut } = useIsCut(row.title);
  const prio = CTEM_PRIORITY_CONFIG[actNowPriorityLabel(row)];
  const button = (
    <button
      type="button"
      onClick={row.onAction}
      aria-label={`${t_i18n(row.cta)}: ${row.title}`}
      className={cn(
        "flex min-h-14 w-full min-w-0 flex-1 items-center gap-3 rounded-sm px-2 py-2 text-left transition-colors hover:bg-elevation-hover",
        BUTTON_ROLE_FOCUS_RING,
      )}
    >
      <span
        aria-hidden
        className="h-2.5 w-2.5 shrink-0 rounded-full"
        style={{ backgroundColor: ACT_NOW_GROUP_FILLS[actNowGroup(row)] }}
        data-testid="ctem-act-now-dot"
        data-group={actNowGroup(row)}
      />
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-2">
          {first && (
            <Chip label={t_i18n("Do first")} severity="info" className="shrink-0" data-testid="ctem-act-now-first" />
          )}
          <span ref={ref} className="block min-w-0 truncate content-base font-medium">
            {row.title}
          </span>
        </span>
        {row.reason && (
          <span className="block min-w-0 truncate content-compact text-default-secondary" data-testid="ctem-act-now-why">
            {row.reason}
          </span>
        )}
      </span>
      {/* A P1 case shows its urgency chip; a fleet row keeps the slot empty (an
          invisible twin) so the chevrons stay aligned with the rows that carry a
          priority (UX round 1 on #920, B2). */}
      {row.caseUrgency ? (
        <span className="shrink-0" data-testid="ctem-act-now-case-urgency">
          <CaseUrgencyChip severity={row.caseUrgency.severity} priority={row.caseUrgency.priority} />
        </span>
      ) : actNowShowsPriority(row) ? (
        <ToneBadge tone={prio.tone} data-testid="ctem-act-now-priority">
          {t_i18n(prio.label)}
        </ToneBadge>
      ) : (
        <ToneBadge tone={prio.tone} aria-hidden className="invisible" data-testid="ctem-act-now-priority-slot">
          {t_i18n(prio.label)}
        </ToneBadge>
      )}
      <ChevronRight className="h-4 w-4 shrink-0 text-default-secondary" aria-hidden />
    </button>
  );
  return (
    <div className={cn("flex min-w-0 items-center gap-1 rounded-sm", first && "bg-accent/[0.06]")}>
      {cut ? <Tip content={row.title}>{button}</Tip> : button}
      {onAskAi ? (
        <AskAiButton label={`${t_i18n("Ask AI")}: ${row.title}`} onClick={onAskAi} />
      ) : (
        askAiSlot && (
          // An invisible twin keeps the Ask AI column the same width on every
          // row, so the priority tag and chevron line up (UX round 1 on #11215, B1).
          <span className="invisible shrink-0" aria-hidden data-testid="ctem-act-now-ask-ai-slot">
            <AskAiButton label="" tabIndex={-1} disabled />
          </span>
        )
      )}
    </div>
  );
}

/** What waits, by source: one segment per source sized by its rows, and a
 *  legend that puts a swatch and the count before each label, so no source is
 *  told by colour alone. */
function ActNowSourceBar({ queue }: { queue: readonly ActNowRow[] }) {
  const { t_i18n } = useFormatter();
  const nf = useNumberFormat();
  const groups = actNowGroupCounts(queue);
  const total = groups.reduce((n, g) => n + g.count, 0);
  if (total === 0) return null;
  const summary = groups
    .map((g) => `${t_i18n(ACT_NOW_GROUP_LABELS[g.group])} ${nf.count(g.count)}`)
    .join(", ");
  return (
    <div className="flex flex-col gap-1.5" data-testid="ctem-act-now-bar">
      <div
        role="img"
        aria-label={t_i18n("What waits, by source: {summary}", { summary })}
        className="flex h-2 w-full gap-0.5 overflow-hidden rounded-sm"
      >
        {groups.map((g) => (
          <span
            key={g.group}
            className="block h-full first:rounded-l-sm last:rounded-r-sm"
            style={{ flexGrow: g.count, flexBasis: 0, backgroundColor: ACT_NOW_GROUP_FILLS[g.group] }}
            data-testid={`ctem-act-now-segment-${g.group}`}
          />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 content-compact text-default-secondary" aria-hidden data-testid="ctem-act-now-legend">
        {groups.map((g) => (
          <li key={g.group} className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: ACT_NOW_GROUP_FILLS[g.group] }} />
            <span className="font-semibold tabular-nums text-default-primary">{nf.count(g.count)}</span>
            {t_i18n(ACT_NOW_GROUP_LABELS[g.group])}
          </li>
        ))}
      </ul>
    </div>
  );
}

function AskAiButton({
  label, onClick, tabIndex, disabled,
}: { label: string; onClick?: () => void; tabIndex?: number; disabled?: boolean }) {
  const { t_i18n } = useFormatter();
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-9 shrink-0 gap-1.5 text-accent"
      onClick={onClick}
      tabIndex={tabIndex}
      disabled={disabled}
      aria-label={label || undefined}
    >
      <Icon name="sparkles" size={16} />
      <span className="hidden @xl:inline">{t_i18n("Ask AI")}</span>
    </Button>
  );
}

export function ActNowCard({ rows, briefingActions = [], aiLockReason, onContentHeight, refreshing = false }: ActNowCardProps) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const [expanded, setExpanded] = useState(false);
  const waiting = useWorkItemsWaitingForMe();
  const approvals = usePendingApprovalsByTool(true, undefined, true);
  // An open P1 case of the loop is "Do first" (CTEM-03-UI, #892); the
  // `ctem_case_update` frame re-reads the list, so it appears without a reload.
  const cases = useCtemCases({ throwOnError: false });
  const now = useNow(60_000);
  // The scope the gate read for each case, from the reader's own signals only
  // (CTEM-30, #937): a case whose signals they cannot read ranks after the others
  // of its severity and names no primary asset.
  const caseSignals = useCtemSignalList("case_opened", 200);
  const scopes = useMemo(() => caseScopes(caseSignals.data?.pages.flatMap((p) => p.signals) ?? []), [caseSignals.data]);
  const loopCases = useMemo(() => actNowCases(cases.data?.cases ?? [], scopes), [cases.data, scopes]);
  // The cases whose remediation decision waits for a person (CTEM-17-UI): the
  // open decision items the reader can see, one read for every case.
  const decisions = useWorkItems({ correlation_key_prefix: REMEDIATION_DECISION_PREFIX, open_only: true, limit: 50 });
  const decisionCases = useMemo(() => {
    const ids = new Set<string>();
    for (const item of decisions.data ?? []) {
      const caseId = caseIdOfDecisionKey(item.correlation_key);
      if (caseId && waitsForAnswer(item)) ids.add(caseId);
    }
    return ids;
  }, [decisions.data]);

  const waitingCount = waiting.data?.count ?? 0;
  const approvalRuns =
    (approvals.data?.tools ?? []).reduce((n, t) => n + t.run_count, 0) + (approvals.data?.mixed_run_count ?? 0);

  const queue = useMemo(() => {
    const extra: ActNowRow[] = [];
    loopCases.forEach((c, index) => {
      const countdown = slaCountdown(c.sla_due_at, now);
      const scope = scopes.get(c.id);
      const parts = [
        decisionCases.has(c.id) ? t_i18n("Remediation decision waits") : "",
        scope?.primaryAssets.length
          ? t_i18n("Primary asset: {name}", { name: scope.primaryAssets.join(", ") })
          : scope?.criticality
            ? t_i18n("Scope criticality: {level}", { level: t_i18n((CRITICALITY_LABELS as Record<string, string>)[scope.criticality] ?? scope.criticality) })
            : "",
        countdown ? slaSentence(countdown, t_i18n, intl.locale) : "",
      ].filter(Boolean);
      extra.push({
        id: `case-${c.id}`, source: "case", icon: Zap,
        tone: c.severity === "P1" ? "text-feedback-error-primary" : "text-feedback-warning-primary",
        title: c.title,
        reason: parts.length > 0 ? parts.join(" · ") : t_i18n("Case opened by the loop"),
        cta: "Open", onAction: () => navigate(`/ctem/cases/${c.id}`),
        priority: actNowCasePriority(c.severity, index),
        caseUrgency: { severity: c.severity ?? null, priority: c.priority },
      });
    });
    if (waitingCount > 0) {
      const oldest = waiting.data?.oldest;
      extra.push({
        id: "work-items-waiting", source: "work_item", icon: Inbox, tone: "text-feedback-warning-primary",
        title: t_i18n("{count, plural, one {{shown} work item waits for you} other {{shown} work items wait for you}}", { count: waitingCount, shown: formatCompactNumber(waitingCount) }),
        reason: oldest?.title ?? t_i18n("A decision or an answer is owed before the work goes on"),
        cta: "Open", onAction: () => navigate(waitingForMePath()),
        priority: ACT_NOW_PRIORITY.work_item,
      });
    }
    if (approvalRuns > 0) {
      extra.push({
        id: "approvals-waiting", source: "approval", icon: ShieldQuestion, tone: "text-feedback-warning-primary",
        title: t_i18n("{count, plural, one {# run waits for your approval} other {# runs wait for your approval}}", { count: approvalRuns }),
        reason: t_i18n("An agent paused on a tool call that needs a person's go"),
        cta: "Review", onAction: () => navigate("/operations/executions"),
        priority: ACT_NOW_PRIORITY.approval,
      });
    }
    return actNowQueue([...rows, ...extra], undefined, briefingActions);
  }, [rows, briefingActions, loopCases, scopes, decisionCases, now, intl.locale, waitingCount, waiting.data?.oldest, approvalRuns, navigate, t_i18n]);

  const shown = expanded ? queue : queue.slice(0, ACT_NOW_COLLAPSED);
  const more = queue.length > ACT_NOW_COLLAPSED;
  const generic = actNowIsGeneric(queue);
  const offersAskAi = (row: ActNowRow) => !aiLockReason && actNowOffersAskAi(row);
  // One Ask AI column for the shown rows: reserved on every row when any has it.
  const askAiSlot = shown.some(offersAskAi);
  const askAi = (row: ActNowRow) => {
    primeChatDraft(actNowAskAiPrompt(t_i18n, row));
    navigate("/chat");
  };

  // The content is measured, not the card: the grid stretches the card to its
  // cell, while the content keeps its own height.
  const contentRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = contentRef.current;
    if (!node || !onContentHeight) return;
    // Plus the card's 1 px border on each side.
    const report = () => onContentHeight(Math.ceil(node.getBoundingClientRect().height) + 2);
    report();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(report);
    observer.observe(node);
    return () => observer.disconnect();
  }, [onContentHeight]);

  return (
    <Card className="flex flex-col" data-testid="ctem-act-now">
      <CardContent ref={contentRef} className="@container flex flex-col gap-2 p-4">
        <h2 className="flex h-9 min-w-0 items-center gap-2 content-base font-semibold">
          <Zap className="h-5 w-5 shrink-0 text-accent" aria-hidden />
          <span className="truncate">{t_i18n("Act now")}</span>
          {refreshing && (
            <span className="ml-auto inline-flex shrink-0 items-center gap-1.5 content-caption font-normal text-default-secondary" role="status" data-testid="ctem-act-now-refreshing">
              <Spinner size="sm" tone="inherit" className="text-accent" />
              {t_i18n("Refreshing...")}
            </span>
          )}
        </h2>

        {generic && (
          <GenericContextNotice
            title={t_i18n("These actions are generic")}
            adminText={t_i18n("They come from module-wide figures, not from your organization context. Set it up to get actions tailored to you.")}
            readerText={t_i18n("They come from module-wide figures, not from your organization context. An administrator can set it up to get actions tailored to you.")}
            testId="ctem-act-now-generic"
          />
        )}

        {queue.length === 0 ? (
          <RecommendedActionsEmpty lockReason={aiLockReason} />
        ) : (
          <>
          <ActNowSourceBar queue={queue} />
          <ol className="flex flex-col divide-y divide-[var(--border-elevation-subtle-soft)]" data-testid="ctem-act-now-rows">
            {shown.map((r, i) => (
              <li key={r.id} data-source={r.source}>
                <ActNowRowButton
                  row={r}
                  first={i === 0}
                  onAskAi={offersAskAi(r) ? () => askAi(r) : undefined}
                  askAiSlot={askAiSlot}
                />
              </li>
            ))}
          </ol>
          </>
        )}

        {more && (
          <Button type="button" variant="ghost" size="sm" className="h-9 self-start text-accent" onClick={() => setExpanded((e) => !e)} aria-expanded={expanded}>
            {expanded ? t_i18n("Show fewer") : t_i18n("Show all ({count})", { count: intl.formatNumber(queue.length) })}
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
