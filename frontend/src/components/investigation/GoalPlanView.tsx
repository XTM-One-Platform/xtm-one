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
 * A Deep Investigation drawn as its goal plan: the default rendering
 * of the investigation block, for every pack.
 *
 * The run is read as a goal-oriented plan - an objective, a world state of
 * predicates, the actions A* chose to reach the goal, each action the steps
 * doing its work - and then the cited report as the plan's last action. A pack
 * that declares a plan gets its own actions; any other run gets the default
 * plan the server reads from its kinds of source. Nothing here names a pack,
 * an action or a predicate: the plan comes from the server, the state from
 * `goal-state.ts`, the look of an action from the icon it declares.
 *
 * The block's honesty rules are kept, not restyled away: an action whose
 * sources found nothing is not "complete", the state never runs ahead of the
 * steps, no metric is shown that was not measured, and the planner card says
 * what the run actually does (sequential, bounded) rather than what a planner
 * could do.
 */
import { type ReactNode, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Circle,
  Clock,
  Database,
  Download,
  FileText,
  Lightbulb,
  Loader2,
  Minus,
  Presentation,
  Route,
  ShieldCheck,
  SkipForward,
  Target,
  TrendingUp,
  X,
} from "lucide-react";

import { ChatMarkdown } from "@/components/chat/ChatMarkdown";
import { ACTION_ICONS } from "@/components/investigation/action-icons";
import { useChatActions } from "@/components/chat/chat-actions";
import { BlockPromptButton } from "@/components/investigation/BlockPromptButton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn, formatDurationShort } from "@/lib/utils";
import { safeHttpUrl } from "@/lib/safe-url";
import type {
  Investigation,
  InvestigationGoalPlan,
  InvestigationStep,
  InvestigationStepStatus,
} from "@/types";

import {
  deriveGoalState,
  type GoalActionState,
  type GoalActionStatus,
  type GoalState,
  type ReportPhase,
  reportLead,
  reportPhase,
} from "./goal-state";
import { Spinner } from "@filigran/design-system";

/** The backend's closed `GOAL_ACTION_ICONS` set, as glyphs. */

/** What an action's status reads as. @i18n-keys */
const ACTION_STATUS_LABELS = {
  pending: "Pending",
  active: "In progress",
  completed: "Complete",
  partial: "Partial",
  empty: "Nothing found",
  failed: "Failed",
  not_reached: "Not reached",
} as const satisfies Record<GoalActionStatus, string>;

/** The action counters under the timeline. @i18n-keys */
const COUNTER_LABELS = {
  completed: "Completed",
  active: "Active",
  pending: "Pending",
  unmet: "Not achieved",
} as const;

/** The report card's tabs. @i18n-keys */
const REPORT_TAB_LABELS = {
  findings: "Key findings",
  method: "Methodology",
  sources: "Sources",
  coverage: "Coverage",
} as const;

const REPORT_TAB_ICONS: Record<keyof typeof REPORT_TAB_LABELS, typeof Target> = {
  findings: Lightbulb,
  method: Route,
  sources: BookOpen,
  coverage: ShieldCheck,
};

/** The full report's tabs. @i18n-keys */
const FULL_REPORT_TAB_LABELS = {
  report: "Report",
  actions: "Actions carried out",
  sources: "Sources",
} as const;

/** The report action's line, per phase. @i18n-keys */
const REPORT_PHASE_TEXT = {
  waiting: "Written once every source has answered",
  writing: "Writing the cited report…",
  written: "Every sentence traceable to a passage read",
  missing: "Nothing the sources returned could back a report",
} as const satisfies Record<ReportPhase, string>;

const ACTION_STYLES: Record<
  GoalActionStatus,
  { card: string; dot: string; badge: "outline" | "cyan" | "success" | "warning" | "destructive" }
> = {
  pending: {
    card: "border-elevation-subtle-soft layer-1 bg-elevation-default",
    dot: "bg-[var(--text-default-secondary)]",
    badge: "outline",
  },
  active: {
    card: "border-accent/60 bg-accent/[0.04] ring-1 ring-accent/30",
    dot: "bg-accent",
    badge: "cyan",
  },
  completed: {
    card: "border-feedback-success-primary/35 bg-feedback-success-primary/[0.05]",
    dot: "bg-feedback-success-primary",
    badge: "success",
  },
  partial: {
    card: "border-feedback-alert-primary/35 bg-feedback-alert-primary/[0.04]",
    dot: "bg-feedback-alert-primary",
    badge: "warning",
  },
  empty: {
    card: "border-elevation-subtle-soft layer-1 bg-elevation-default",
    dot: "bg-[var(--text-default-secondary)]",
    badge: "outline",
  },
  failed: {
    card: "border-feedback-error-primary/30 bg-feedback-error-primary/[0.04]",
    dot: "bg-feedback-error-primary",
    badge: "destructive",
  },
  not_reached: {
    card: "border-dashed border-elevation-subtle-soft layer-1 bg-elevation-default",
    dot: "bg-[var(--text-default-secondary)]",
    badge: "outline",
  },
};

const STEP_ICONS: Record<InvestigationStepStatus, { icon: typeof Check; tone: string; spin?: boolean }> = {
  pending: { icon: Clock, tone: "text-default-secondary" },
  active: { icon: Loader2, tone: "text-accent", spin: true },
  completed: { icon: Check, tone: "text-feedback-success-primary" },
  empty: { icon: Minus, tone: "text-default-secondary" },
  degraded: { icon: AlertTriangle, tone: "text-feedback-warning-primary" },
  error: { icon: X, tone: "text-feedback-error-primary" },
  skipped: { icon: SkipForward, tone: "text-default-secondary" },
};

type Translate = (id: string, values?: Record<string, string | number>) => string;

interface GoalPlanViewProps {
  run: Investigation;
  plan: InvestigationGoalPlan;
  /** The run is still querying (and not stalled). */
  live: boolean;
  /** The run ended and its report may still be on its way. */
  awaitingReport: boolean;
  /** The block's own rendering of one step, shown when a row is opened. */
  renderStep: (step: InvestigationStep) => ReactNode;
  /** The block's one-line coverage account (`12 sources · 9 found …`). */
  coverage: string;
}

export function GoalPlanView({ run, plan, live, awaitingReport, renderStep, coverage }: GoalPlanViewProps) {
  const { t_i18n, t_text } = useFormatter();
  const phase = reportPhase(run, { live, awaiting: awaitingReport });
  const state = useMemo(
    () => deriveGoalState(plan, run.steps, { live, report: phase }),
    [plan, run.steps, live, phase],
  );
  const labelOf = useMemo(() => {
    const map = new Map(plan.predicates.map((p) => [p.slug, p.label]));
    return (slug: string) => t_text(map.get(slug) ?? slug);
  }, [plan.predicates, t_text]);

  return (
    <div className="@container space-y-3 border-t border-elevation-subtle-soft p-3">
      <div className="flex flex-wrap items-baseline justify-center gap-x-1.5 text-center content-compact">
        <span className="font-semibold text-default-primary">{t_i18n("Objective:")}</span>
        <span className="text-default-secondary">{t_i18n(plan.objective, { value: run.subject_value })}</span>
      </div>

      <div className="grid gap-3 @2xl:grid-cols-[3fr_2fr]">
        <StateAssessment plan={plan} state={state} labelOf={labelOf} t_i18n={t_i18n} />
        <PlannerCard run={run} plan={plan} state={state} t_i18n={t_i18n} />
      </div>

      <ol className="relative space-y-3 pl-6">
        <span aria-hidden className="absolute bottom-3 left-[7px] top-3 w-px bg-[var(--border-elevation-subtle)]" />
        {state.actions.map((a, index) => (
          <ActionCard
            key={a.action.slug}
            entry={a}
            index={index}
            phase={phase}
            renderStep={renderStep}
            t_i18n={t_i18n}
          />
        ))}
      </ol>

      {state.unplanned.length > 0 && (
        <div className="rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default">
          <div className="px-3 py-2 text-[11px] font-medium text-default-secondary">
            {t_i18n("Other sources")}
          </div>
          <StepRows steps={state.unplanned} renderStep={renderStep} t_i18n={t_i18n} />
        </div>
      )}

      <Counters state={state} t_i18n={t_i18n} />

      <ReportCard
        run={run}
        state={state}
        phase={phase}
        coverage={coverage}
        objective={t_i18n(plan.objective, { value: run.subject_value })}
        t_i18n={t_i18n}
      />
    </div>
  );
}

function StateAssessment({
  plan,
  state,
  labelOf,
  t_i18n,
}: {
  plan: InvestigationGoalPlan;
  state: GoalState;
  labelOf: (slug: string) => string;
  t_i18n: Translate;
}) {
  const { t_text } = useFormatter();
  const pct = state.goalTotal ? Math.round((state.goalMet / state.goalTotal) * 100) : 0;
  const goal = new Set(plan.goal_state);
  const next = state.remaining[0]?.slug;
  return (
    <section className="rounded-sm border border-accent/30 layer-1 bg-elevation-default p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="flex items-center gap-1.5 content-base font-semibold text-default-primary">
            <Target className="h-4 w-4 text-accent" />
            {t_i18n("Goal state assessment")}
          </h3>
          <p className="mt-0.5 content-caption text-default-secondary">
            {t_i18n("Read from what the sources answered, step by step")}
          </p>
        </div>
        <div className="text-right">
          <div className="text-xl font-bold tabular-nums text-accent">{pct}%</div>
          <div className="content-caption text-default-secondary">
            {t_i18n("{met}/{total} goal conditions", { met: state.goalMet, total: state.goalTotal })}
          </div>
        </div>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-elevation-highlight">
        <div
          className="h-full rounded-full bg-gradient-to-r from-accent/70 to-accent transition-[width] duration-700"
          style={{ width: `${pct}%` }}
        />
      </div>

      <div className="mt-3 flex items-center justify-between content-caption font-medium text-default-secondary">
        <span className="flex items-center gap-1">
          <Circle className="h-3 w-3" />
          {t_i18n("World state")}
        </span>
        <Badge variant="outline" className="px-1.5 py-0 text-[9px]">
          <CheckCircle2 className="mr-1 h-2.5 w-2.5 text-feedback-success-primary" />
          {t_i18n("Goal")}
        </Badge>
      </div>
      <ul className="mt-1.5 space-y-1">
        {plan.predicates.map((p) => {
          const holds = !!state.holds[p.slug];
          return (
            <li
              key={p.slug}
              className={cn(
                "flex items-center gap-2 rounded-sm border px-2 py-1 transition-colors duration-500",
                holds ? "border-feedback-success-primary/30 bg-feedback-success-primary/[0.06]" : "border-elevation-subtle-soft",
              )}
            >
              {holds ? (
                <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-feedback-success-primary" />
              ) : (
                <Circle className="h-3.5 w-3.5 shrink-0 text-default-secondary" />
              )}
              <span className="min-w-0 flex-1 truncate text-[11px] text-default-primary">{labelOf(p.slug)}</span>
              {state.given.has(p.slug) ? (
                <Tip content={t_i18n("Already true for this subject before the run")}>
                  <span className="rounded-sm bg-elevation-highlight px-1 text-[9px] text-default-secondary">{t_i18n("given")}</span>
                </Tip>
              ) : (
                <code
                  className={cn(
                    "rounded-sm px-1 text-[9px]",
                    holds ? "bg-feedback-success-primary/20 text-feedback-success-primary" : "bg-elevation-highlight text-default-secondary",
                  )}
                >
                  {holds ? "true" : "false"}
                </code>
              )}
              {goal.has(p.slug) && (
                <Tip content={t_i18n("A condition of the goal")}>
                  <Target className="h-3 w-3 shrink-0 text-accent/70" />
                </Tip>
              )}
            </li>
          );
        })}
      </ul>

      {state.transitions.length > 0 && (
        <div className="mt-3">
          <div className="flex items-center gap-1 content-caption font-medium text-default-secondary">
            <ArrowRight className="h-3 w-3 text-accent" />
            {t_i18n("State transitions")}
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {state.transitions.map((tr) => (
              <span
                key={tr.predicate}
                className="animate-fade-in inline-flex items-center gap-1 rounded-sm border border-elevation-subtle-soft px-2 py-0.5 text-[9px] text-default-secondary"
              >
                {labelOf(tr.predicate)}
                <code className="text-default-secondary">false</code>
                <ArrowRight className="h-2.5 w-2.5" />
                <code className="text-feedback-success-primary">true</code>
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="mt-3 border-t border-elevation-subtle-soft pt-2">
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-1 text-[11px] font-medium text-default-primary">
            <TrendingUp className="h-3 w-3 text-accent" />
            {t_i18n("Action plan")}
          </span>
          <Badge variant="outline" className="px-1.5 py-0 text-[9px]">
            {state.remaining.length === 1
              ? t_i18n("1 action remaining")
              : t_i18n("{count} actions remaining", { count: state.remaining.length })}
          </Badge>
        </div>
        {plan.reachable === false ? (
          <p className="mt-1.5 flex items-start gap-1 content-caption text-feedback-warning-primary">
            <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
            {t_i18n(
              plan.actions.some((a) => a.servable === false)
                ? "Some actions have no source in this run, so the goal cannot be reached in full."
                : "No sequence of this plan's actions reaches the goal for this subject.",
            )}
          </p>
        ) : state.remaining.length > 0 ? (
          <ol className="mt-1.5 space-y-1">
            {state.remaining.map((action) => (
              <li
                key={action.slug}
                className={cn(
                  "flex items-center gap-2 rounded-sm border px-2 py-1 text-[11px]",
                  action.slug === next ? "border-accent/40 text-default-primary" : "border-elevation-subtle-soft text-default-secondary",
                )}
              >
                <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-accent/15 text-[9px] font-semibold text-accent">
                  {plan.actions.findIndex((a) => a.slug === action.slug) + 1}
                </span>
                <span className="truncate">{t_text(action.label)}</span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="mt-1.5 content-caption text-default-secondary">{t_i18n("Nothing left to plan.")}</p>
        )}
      </div>
    </section>
  );
}

function PlannerCard({
  run,
  plan,
  state,
  t_i18n,
}: {
  run: Investigation;
  plan: InvestigationGoalPlan;
  state: GoalState;
  t_i18n: Translate;
}) {
  const hops = run.steps.filter((s) => s.lead_from_step_id).length;
  const rows: [string, string][] = [
    [
      t_i18n("Planner"),
      plan.declared ? t_i18n("A* over the pack's actions") : t_i18n("Default plan, one action per kind of source"),
    ],
    [t_i18n("Path"), t_i18n("{count} actions · cost {cost}", { count: plan.actions.length, cost: plan.path_cost })],
    [t_i18n("Execution"), t_i18n("Sequential, in plan order, in one run")],
    [t_i18n("Replanning"), t_i18n("{count} steps added by what the sources found", { count: hops })],
    [t_i18n("Budget"), t_i18n("{used}/{max} queries", { used: run.iterations_used, max: run.max_iterations })],
  ];
  return (
    <section className="rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-3">
      <h3 className="flex items-center gap-1.5 content-base font-semibold text-default-primary">
        <Route className="h-4 w-4 text-accent" />
        {t_i18n("Planner")}
      </h3>
      <p className="mt-0.5 content-caption text-default-secondary">{t_i18n("How this run is carried out")}</p>
      <dl className="mt-2 space-y-1.5">
        {rows.map(([term, value]) => (
          <div key={term} className="grid grid-cols-[6rem_1fr] gap-2 text-[11px]">
            <dt className="text-default-secondary">{term}</dt>
            <dd className="text-default-primary">{value}</dd>
          </div>
        ))}
      </dl>
      {run.leads_dropped > 0 && (
        <p className="mt-2 flex items-start gap-1 content-caption text-feedback-warning-primary">
          <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
          {t_i18n("{count, plural, one {# lead} other {# leads}} dropped at the plan cap", { count: run.leads_dropped })}
        </p>
      )}
      {state.counts.unmet > 0 && !state.remaining.length && (
        <p className="mt-2 flex items-start gap-1 content-caption text-default-secondary">
          <AlertTriangle className="mt-px h-3 w-3 shrink-0 text-feedback-warning-primary" />
          {t_i18n("Part of the goal was not reached: see the actions below.")}
        </p>
      )}
    </section>
  );
}

function ActionCard({
  entry,
  index,
  phase,
  renderStep,
  t_i18n,
}: {
  entry: GoalActionState;
  index: number;
  phase: ReportPhase;
  renderStep: (step: InvestigationStep) => ReactNode;
  t_i18n: Translate;
}) {
  const { t_text } = useFormatter();
  const { action, status, steps, findings } = entry;
  const style = ACTION_STYLES[status];
  const Icon = ACTION_ICONS[action.icon] ?? Target;
  return (
    <li className="relative" data-goal-action={action.slug} data-goal-status={status}>
      <span
        aria-hidden
        className={cn("absolute -left-6 top-4 h-3.5 w-3.5 rounded-full border-2 border-[var(--bg-elevation-default)]", style.dot)}
      >
        {status === "active" && <span className="absolute inset-0 animate-ping rounded-full bg-accent/60" />}
      </span>
      <div className={cn("rounded-sm border p-3 transition-colors duration-500", style.card)}>
        <div className="flex items-start gap-2.5">
          <div
            className={cn(
              "flex h-8 w-8 shrink-0 items-center justify-center rounded-sm",
              status === "completed" ? "bg-feedback-success-primary/15 text-feedback-success-primary" : "bg-elevation-highlight text-accent",
            )}
          >
            {status === "active" && <Spinner size="sm" tone="inherit" />}{!(status === "active") && <Icon className="h-4 w-4" />}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-2">
              <h4 className="content-compact font-semibold text-default-primary">
                <span className="mr-1.5 text-default-secondary">{index + 1}.</span>
                {t_text(action.label)}
              </h4>
              <Badge variant={style.badge} className="shrink-0 px-1.5 py-0 text-[9px]">
                {t_i18n(
                  action.produces_report && status === "failed"
                    ? "Not written"
                    : action.servable === false && steps.length === 0
                      ? "No source available"
                      : ACTION_STATUS_LABELS[status],
                )}
              </Badge>
            </div>
            <p className="mt-0.5 text-[11px] text-default-secondary">{t_text(action.description)}</p>
            {status === "active" && (
              <div className="mt-2 h-0.5 overflow-hidden rounded-full bg-accent/15">
                <div className="h-full w-full animate-pulse rounded-full bg-accent/70" />
              </div>
            )}
          </div>
        </div>

        {steps.length > 0 && <StepRows steps={steps} renderStep={renderStep} t_i18n={t_i18n} />}

        {action.produces_report ? (
          <ReportPhaseLine phase={phase} t_i18n={t_i18n} />
        ) : (
          <div className="mt-2 flex gap-3 pl-10 content-caption text-default-secondary">
            <span>
              {t_i18n("Sources:")} <strong className="text-default-primary">{steps.length}</strong>
            </span>
            <span>
              {t_i18n("Findings:")} <strong className="text-default-primary">{findings}</strong>
            </span>
          </div>
        )}
      </div>
    </li>
  );
}

function ReportPhaseLine({ phase, t_i18n }: { phase: ReportPhase; t_i18n: Translate }) {
  return <p className="mt-2 pl-10 content-caption text-default-secondary">{t_i18n(REPORT_PHASE_TEXT[phase])}</p>;
}

function StepRows({
  steps,
  renderStep,
  t_i18n,
}: {
  steps: InvestigationStep[];
  renderStep: (step: InvestigationStep) => ReactNode;
  t_i18n: Translate;
}) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <ul className="mt-2 space-y-0.5 pl-10">
      {steps.map((step, i) => {
        const key = step.step_id ?? `${step.position}`;
        const glyph = STEP_ICONS[step.step_status] ?? STEP_ICONS.pending;
        const Glyph = glyph.icon;
        const isOpen = open === key;
        const Chevron = isOpen ? ChevronDown : ChevronRight;
        return (
          <li key={key} className="animate-fade-in" style={{ animationDelay: `${Math.min(i, 8) * 60}ms` }}>
            <button
              type="button"
              onClick={() => setOpen(isOpen ? null : key)}
              aria-expanded={isOpen}
              className="flex w-full items-center gap-1.5 rounded-sm px-1 py-0.5 text-left hover:bg-elevation-highlight"
            >
              <Glyph className={cn("h-3 w-3 shrink-0", glyph.tone, glyph.spin && "animate-spin")} />
              <span
                className={cn(
                  "truncate text-[11px]",
                  step.step_status === "completed" ? "text-feedback-success-primary" : "text-default-primary",
                )}
              >
                {step.source_name}
              </span>
              {step.subject_value && (
                <code className="truncate rounded-sm bg-elevation-highlight px-1 text-[9px] text-default-secondary">
                  {step.subject_value}
                </code>
              )}
              <span className="ml-auto flex shrink-0 items-center gap-1 text-[9px] text-default-secondary">
                {step.findings_count > 0 &&
                  (step.findings_count === 1
                    ? t_i18n("1 finding")
                    : t_i18n("{count} findings", { count: step.findings_count }))}
                <Chevron className="h-3 w-3" />
              </span>
            </button>
            {isOpen && (
              <ul className="my-1 rounded-sm border border-elevation-subtle-soft bg-elevation-default">{renderStep(step)}</ul>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function Counters({ state, t_i18n }: { state: GoalState; t_i18n: Translate }) {
  const tiles: [keyof typeof COUNTER_LABELS, number, string][] = [
    ["completed", state.counts.completed, "text-feedback-success-primary"],
    ["active", state.counts.active, "text-accent"],
    ["pending", state.counts.pending, "text-default-secondary"],
  ];
  if (state.counts.unmet > 0) tiles.push(["unmet", state.counts.unmet, "text-feedback-warning-primary"]);
  return (
    <div className={cn("grid gap-2", tiles.length === 4 ? "grid-cols-4" : "grid-cols-3")}>
      {tiles.map(([label, value, tone]) => (
        <div key={label} className="rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-2 py-2 text-center">
          <div className={cn("text-lg font-semibold tabular-nums", tone)}>{value}</div>
          <div className="content-caption text-default-secondary">{t_i18n(COUNTER_LABELS[label])}</div>
        </div>
      ))}
    </div>
  );
}

type ReportTab = "findings" | "method" | "sources" | "coverage";

function ReportCard({
  run,
  state,
  phase,
  coverage,
  objective,
  t_i18n,
}: {
  run: Investigation;
  state: GoalState;
  phase: ReportPhase;
  coverage: string;
  objective: string;
  t_i18n: Translate;
}) {
  const [tab, setTab] = useState<ReportTab>("findings");
  const [full, setFull] = useState(false);

  if (phase === "waiting") return null;
  if (phase === "writing") {
    return (
      <div className="flex items-center gap-2 rounded-sm border border-accent/30 layer-1 bg-elevation-default px-3 py-3 content-compact text-default-secondary">
        <Spinner size="sm" tone="inherit" className="text-accent" />
        {t_i18n("Writing the cited report…")}
      </div>
    );
  }
  if (phase === "missing" || !run.report) {
    return (
      <div className="flex items-start gap-2 rounded-sm border border-dashed border-elevation-subtle-soft px-3 py-3 text-[11px] text-default-secondary">
        <FileText className="mt-px h-3.5 w-3.5 shrink-0" />
        {t_i18n("No report: nothing the sources returned could back one. The steps above stand on their own.")}
      </div>
    );
  }

  const queried = run.steps.filter((s) => s.step_status !== "pending" && s.step_status !== "skipped").length;
  const findings = run.steps.reduce((sum, s) => sum + (s.findings_count || 0), 0);
  const metrics: [string, string, string?][] = [
    [t_i18n("Actions"), String(state.actions.length)],
    [t_i18n("Sources queried"), String(queried)],
    [t_i18n("Findings"), String(findings)],
  ];
  // Measured values only: no confidence without a sample, no duration
  // without the wall clock.
  if (run.summary.confidence !== null) {
    metrics.push([
      t_i18n("Confidence"),
      `${Math.round(run.summary.confidence * 100)}%`,
      t_i18n("Mean over {count} steps", { count: run.summary.confidence_samples }),
    ]);
  }
  if (run.duration_seconds !== null) metrics.push([t_i18n("Duration"), formatDurationShort(run.duration_seconds)]);

  const tabs = Object.keys(REPORT_TAB_LABELS) as ReportTab[];

  return (
    <div className="animate-fade-in-up space-y-3">
      <section className="rounded-sm border border-feedback-success-primary/35 bg-feedback-success-primary/[0.05] p-3">
        <div className="flex items-start gap-2.5">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-sm bg-feedback-success-primary/15 text-feedback-success-primary">
            <FileText className="h-4 w-4" />
          </div>
          <div className="min-w-0">
            <h3 className="flex items-center gap-1.5 content-base font-semibold text-feedback-success-primary">
              {t_i18n("Final investigation report")}
              <CheckCircle2 className="h-4 w-4" />
            </h3>
            <p className="text-[11px] text-default-secondary">
              {t_i18n("One cited synthesis of every step, written once the run ended")}
            </p>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 @lg:grid-cols-5">
          {metrics.map(([label, value, hint]) => (
            <div key={label} className="rounded-sm bg-elevation-highlight px-2 py-1.5">
              <div className="content-caption text-default-secondary">{label}</div>
              <Tip content={hint ?? label}>
                <div className="text-base font-semibold tabular-nums text-default-primary">{value}</div>
              </Tip>
            </div>
          ))}
        </div>
      </section>

      <section className="rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-3">
        <h3 className="flex items-center gap-1.5 content-compact font-semibold text-default-primary">
          <Target className="h-3.5 w-3.5 text-accent" />
          {t_i18n("In brief")}
        </h3>
        <p className="mt-1.5 text-[12px] font-medium text-feedback-success-primary">{objective}</p>
        <ChatMarkdown content={reportLead(run.report)} className="chat-markdown mt-1 text-[12px]" />
      </section>

      <div className="rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default">
        <div className="grid grid-cols-4 gap-1 border-b border-elevation-subtle-soft p-1">
          {tabs.map((key) => {
            const TabIcon = REPORT_TAB_ICONS[key];
            return (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                aria-pressed={tab === key}
                className={cn(
                  "flex items-center justify-center gap-1.5 rounded-sm px-2 py-1 text-[11px] transition-colors",
                  tab === key ? "bg-elevation-highlight text-default-primary" : "text-default-secondary hover:text-default-primary",
                )}
              >
                <TabIcon className="h-3 w-3" />
                {t_i18n(REPORT_TAB_LABELS[key])}
              </button>
            );
          })}
        </div>
        <div className="max-h-72 overflow-y-auto p-3">
          {tab === "findings" && <FindingsTab run={run} t_i18n={t_i18n} />}
          {tab === "method" && <MethodTab state={state} t_i18n={t_i18n} />}
          {tab === "sources" && <SourcesTab run={run} t_i18n={t_i18n} />}
          {tab === "coverage" && <CoverageTab run={run} coverage={coverage} t_i18n={t_i18n} />}
        </div>
      </div>

      <div
        className={cn(
          "flex flex-wrap items-center justify-between gap-2 rounded-sm border px-3 py-2",
          run.summary.all_succeeded ? "border-feedback-success-primary/35" : "border-feedback-alert-primary/30",
        )}
      >
        {run.summary.all_succeeded ? (
          <span className="flex items-center gap-1.5 text-[11px] text-feedback-success-primary">
            <Check className="h-3.5 w-3.5" />
            {t_i18n("Every source answered with findings")}
          </span>
        ) : (
          <span className="flex items-center gap-1.5 text-[11px] text-default-secondary">
            <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary" />
            {t_i18n("Coverage is incomplete: read the per-action outcomes before relying on this.")}
          </span>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => setFull(true)} className="h-7 gap-1.5 content-compact">
            <FileText className="h-3.5 w-3.5" />
            {t_i18n("View full report")}
          </Button>
          <SendToOpenCTIButton run={run} t_i18n={t_i18n} />
          <ExecutiveSummaryButton run={run} t_i18n={t_i18n} />
        </div>
      </div>

      <FullReportDialog
        open={full}
        onOpenChange={setFull}
        run={run}
        state={state}
        objective={objective}
        t_i18n={t_i18n}
      />
    </div>
  );
}

/**
 * "Send to OpenCTI": the conversation's agent records what the run established
 * in OpenCTI with the OpenCTI integration's tools, in a draft the analyst
 * reviews and validates there. The instruction is the server's; which objects
 * to create comes from the run's steps, through `read_investigation_report`.
 */
function SendToOpenCTIButton({ run, t_i18n }: { run: Investigation; t_i18n: Translate }) {
  const chat = useChatActions();
  const [requested, setRequested] = useState(false);
  const request = run.opencti_record;
  if (!chat || !request) return null;
  return (
    <BlockPromptButton
      tip={t_i18n("The agent records the findings in a new OpenCTI draft, to review and validate in OpenCTI")}
      label={t_i18n("Send to OpenCTI (draft)")}
      requestedLabel={t_i18n("Sent to OpenCTI")}
      icon={<Database className="h-3.5 w-3.5" />}
      requested={requested}
      variant="outline"
      action="opencti-draft"
      onClick={() => {
        setRequested(true);
        chat.sendPrompt(request.prompt, {
          builtinToolIds: ["deep_investigation"],
          integrationIds: [request.integration_id],
        });
      }}
    />
  );
}

/** Tools the agent needs to make the deck: the Presentation feature, the
 *  template lookup that resolves the template the instruction names, and the
 *  investigation suite whose `read_investigation_report` hands it the report.
 *  The server keeps only those the user may add to this agent's turn. */
const EXECUTIVE_SUMMARY_TOOLS = ["generate_presentation", "template_search", "deep_investigation"];

/**
 * "Generate executive summary": the conversation's agent makes a presentation
 * of this report with the XTM One template, through the Presentation feature.
 * The instruction is the server's (`executive_summary.prompt`), sent as the
 * next message; the deck arrives in the chat as the agent's answer. Offered
 * only inside a chat that can send (`useChatActions`).
 */
function ExecutiveSummaryButton({ run, t_i18n }: { run: Investigation; t_i18n: Translate }) {
  const chat = useChatActions();
  const [requested, setRequested] = useState(false);
  const request = run.executive_summary;
  if (!chat || !request) return null;
  return (
    <BlockPromptButton
      tip={t_i18n("The agent makes a presentation of this report with the {template} template", {
        template: request.template_name,
      })}
      label={t_i18n("Generate executive summary")}
      requestedLabel={t_i18n("Executive summary requested")}
      icon={<Presentation className="h-3.5 w-3.5" />}
      requested={requested}
      action="executive-summary"
      onClick={() => {
        setRequested(true);
        chat.sendPrompt(request.prompt, { builtinToolIds: EXECUTIVE_SUMMARY_TOOLS });
      }}
    />
  );
}

type FullReportTab = keyof typeof FULL_REPORT_TAB_LABELS;

/** The report as one Markdown file: the prose, then the numbered key. */
function reportMarkdown(run: Investigation): string {
  const key = (run.report_sources ?? []).map((s) => {
    const href = safeHttpUrl(s.href);
    return `${s.n}. ${s.label}${href ? ` - ${href}` : ""} (${s.source_name})`;
  });
  return [run.report ?? "", key.length ? `## Sources\n\n${key.join("\n")}` : ""].filter(Boolean).join("\n\n");
}

function downloadReport(run: Investigation): void {
  const name = run.subject_value.replace(/[^a-z0-9.-]+/gi, "-").replace(/^-+|-+$/g, "") || "subject";
  const url = URL.createObjectURL(new Blob([reportMarkdown(run)], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `investigation-${name}.md`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function FullReportDialog({
  open,
  onOpenChange,
  run,
  state,
  objective,
  t_i18n,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  run: Investigation;
  state: GoalState;
  objective: string;
  t_i18n: Translate;
}) {
  const [tab, setTab] = useState<FullReportTab>("report");
  const sources = run.report_sources?.length ?? 0;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader className="pr-8">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <DialogTitle>{t_i18n("Investigation report")}</DialogTitle>
              <DialogDescription className="mt-1">{objective}</DialogDescription>
            </div>
            <Button size="sm" variant="outline" onClick={() => downloadReport(run)} className="h-7 gap-1.5 content-compact">
              <Download className="h-3.5 w-3.5" />
              {t_i18n("Download as Markdown")}
            </Button>
          </div>
        </DialogHeader>
        <div className="flex gap-1 rounded-sm border border-elevation-subtle-soft p-1">
          {(Object.keys(FULL_REPORT_TAB_LABELS) as FullReportTab[]).map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              aria-pressed={tab === key}
              className={cn(
                "flex-1 rounded-sm px-2 py-1 content-compact transition-colors",
                tab === key ? "bg-elevation-highlight text-default-primary" : "text-default-secondary hover:text-default-primary",
              )}
            >
              {t_i18n(FULL_REPORT_TAB_LABELS[key])}
              {key === "sources" && ` (${sources})`}
            </button>
          ))}
        </div>
        {tab === "report" && <ChatMarkdown content={run.report ?? ""} />}
        {tab === "actions" && <ActionCards state={state} t_i18n={t_i18n} />}
        {tab === "sources" && <SourcesTab run={run} t_i18n={t_i18n} />}
      </DialogContent>
    </Dialog>
  );
}

/** Every action of the plan as a card: what it did and through how many sources. */
function ActionCards({ state, t_i18n }: { state: GoalState; t_i18n: Translate }) {
  const { t_text } = useFormatter();
  return (
    <ul className="space-y-2">
      {state.actions.map((a) => {
        const Icon = ACTION_ICONS[a.action.icon] ?? Target;
        const done = a.status === "completed" || a.status === "partial";
        return (
          <li key={a.action.slug} className="flex items-start gap-3 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-3 py-2.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-elevation-highlight text-accent">
              <Icon className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="content-compact font-semibold text-default-primary">{t_text(a.action.label)}</div>
              <div className="text-[11px] text-default-secondary">{t_text(a.action.description)}</div>
            </div>
            <Badge variant={done ? "success" : ACTION_STYLES[a.status].badge} className="shrink-0 px-2 py-0 content-caption">
              {a.action.produces_report
                ? t_i18n(ACTION_STATUS_LABELS[a.status])
                : a.steps.length === 1
                  ? t_i18n("1 source")
                  : t_i18n("{count} sources", { count: a.steps.length })}
            </Badge>
          </li>
        );
      })}
    </ul>
  );
}

function FindingsTab({ run, t_i18n }: { run: Investigation; t_i18n: Translate }) {
  const found = run.steps.filter((s) => s.findings_count > 0);
  if (!found.length) return <p className="text-[11px] text-default-secondary">{t_i18n("No source found anything.")}</p>;
  return (
    <ul className="space-y-1.5">
      {found.map((s) => (
        <li key={s.step_id ?? s.position} className="flex items-start gap-2 text-[11px]">
          <Check className="mt-0.5 h-3 w-3 shrink-0 text-feedback-success-primary" />
          <div className="min-w-0">
            <span className="font-medium text-default-primary">{s.source_name}</span>
            {s.subject_value && <code className="ml-1.5 content-caption text-default-secondary">{s.subject_value}</code>}
            <span className="ml-1.5 text-default-secondary">
              {s.findings_count === 1 ? t_i18n("1 finding") : t_i18n("{count} findings", { count: s.findings_count })}
            </span>
            {s.evidence[0] && <div className="truncate content-caption text-default-secondary/80">{s.evidence[0].label}</div>}
          </div>
        </li>
      ))}
    </ul>
  );
}

function MethodTab({ state, t_i18n }: { state: GoalState; t_i18n: Translate }) {
  const { t_text } = useFormatter();
  return (
    <ol className="space-y-2">
      {state.actions.map((a, i) => (
        <li key={a.action.slug} className="text-[11px]">
          <div className="flex items-center gap-2">
            <span className="text-default-secondary">{i + 1}.</span>
            <span className="font-medium text-default-primary">{t_text(a.action.label)}</span>
            <Badge variant={ACTION_STYLES[a.status].badge} className="px-1.5 py-0 text-[9px]">
              {t_i18n(ACTION_STATUS_LABELS[a.status])}
            </Badge>
          </div>
          {a.steps.length > 0 && (
            <div className="pl-4 content-caption text-default-secondary">
              {[...new Set(a.steps.map((s) => s.source_name))].join(" · ")}
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}


function SourcesTab({ run, t_i18n }: { run: Investigation; t_i18n: Translate }) {
  const sources = run.report_sources ?? [];
  if (!sources.length) return <p className="text-[11px] text-default-secondary">{t_i18n("The report cites no passage.")}</p>;
  return (
    <ol className="space-y-1.5">
      {sources.map((s) => {
        const href = safeHttpUrl(s.href);
        return (
          <li key={s.n} className="flex items-start gap-2 text-[11px]">
            <span className="shrink-0 font-mono-plex content-caption text-accent">[{s.n}]</span>
            <div className="min-w-0">
              {href ? (
                <a
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="break-all text-default-primary underline decoration-dotted underline-offset-2"
                >
                  {s.label}
                </a>
              ) : (
                <span className="text-default-primary">{s.label}</span>
              )}
              <span className="ml-1.5 content-caption text-default-secondary">{s.source_name}</span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function CoverageTab({ run, coverage, t_i18n }: { run: Investigation; coverage: string; t_i18n: Translate }) {
  return (
    <div className="space-y-1.5 text-[11px] text-default-secondary">
      <p className="text-default-primary">{coverage}</p>
      {typeof run.report_claims === "number" && typeof run.report_uncited_claims === "number" && (
        <p>
          {t_i18n("{uncited} of {claims} assertive sentences carry no citation", {
            uncited: run.report_uncited_claims,
            claims: run.report_claims,
          })}
        </p>
      )}
      {run.leads_dropped > 0 && <p>{t_i18n("{count, plural, one {# lead} other {# leads}} dropped at the plan cap", { count: run.leads_dropped })}</p>}
    </div>
  );
}
