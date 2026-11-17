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
 * The chat surface for an `[[INVESTIGATION:uuid]]` marker.
 *
 * A collapsible block listing every source the run planned, queried, or never
 * reached — one assistant message with a collapsible block, not a series of
 * messages: the run is one unit of work producing one artifact.
 *
 * The whole design brief is that an analyst can tell a thorough investigation
 * from a degraded one **at a glance**, so three rules govern the rendering and
 * none of them are cosmetic:
 *
 * 1. **A non-success never looks like a success.** Seven step states get seven
 *    treatments, and the three that are easy to conflate are kept apart on
 *    purpose: `completed` (found something), `empty` (answered, found nothing)
 *    and `degraded` (only partly read). The prior art that motivated this issue
 *    rendered all of them — plus outright failures — as one green "Complete".
 * 2. **No metric is shown that was not measured.** Confidence renders only when
 *    the payload carries one; duration only from `duration_seconds`, which the
 *    backend computes from wall clock and leaves null while a run is unfinished.
 *    There is no client-side fallback for either, deliberately.
 * 3. **The summary cannot overclaim.** The "all sources answered" line is gated
 *    on the server-computed `summary.all_succeeded`, never on a count this
 *    component derives, so one rule decides it here, on the completion frame
 *    and in the report.
 *
 * Every run is drawn as its goal plan (`GoalPlanView`): the objective, the
 * world state, the actions and the steps doing each one, then the report. The
 * flat step list below is kept for a server too old to send a plan.
 *
 * Data flow: the block hydrates from `GET /investigations/{id}` and stays
 * correct with zero stream frames — a reload mid-run, a late join or a dropped
 * stream all render the same block. While the run is unfinished the query
 * polls; frames are the low-latency path over the same shape (see
 * `useInvestigationApi`).
 */
import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  Loader2,
  Minus,
  Search,
  SkipForward,
  X,
} from "lucide-react";

import { UnavailableCard } from "@/components/chat/UnavailableCard";
import { Badge } from "@/components/ui/badge";
import { CitedAnswer } from "@/components/investigation/CitedAnswer";
import { ContinuationPanel } from "@/components/investigation/ContinuationPanel";
import { GoalPlanView } from "@/components/investigation/GoalPlanView";
import { markSeenLive, wasSeenLive } from "@/components/investigation/seen-live";
import { Tip } from "@/components/ui/tooltip";
import {
  awaitingReport,
  REPORT_WAIT_MS,
  useInvestigation,
} from "@/hooks/api/useInvestigationApi";
import { useFormatter } from "@/i18n";
import { cn, formatDurationShort } from "@/lib/utils";
import { safeHttpUrl } from "@/lib/safe-url";
import type {
  Investigation,
  InvestigationDetailCode,
  InvestigationErrorType,
  InvestigationEvidence,
  InvestigationStep,
  InvestigationStepStatus,
} from "@/types";

import { detailMessage } from "./detail-messages";
import { Spinner } from "@filigran/design-system";

/**
 * Per-status presentation. One table so a new state cannot be added to the
 * backend without a visible gap here, and so the "never looks like a success"
 * rule is auditable in one place rather than spread across JSX branches.
 */
const STEP_STYLES: Record<
  InvestigationStepStatus,
  {
    icon: typeof Check;
    /** Icon colour. Only `completed` is green. */
    tone: string;
    label: string;
    spin?: boolean;
  }
> = {
  // "Planned step", not "Planned": main's speaker view renders "Planned" for a
  // planned TIME, and one English key cannot carry two genders in French — it
  // qualifies an "étape" here and a masculine noun there. Sharing the key made
  // one of the two features read wrong, whichever value won the merge.
  pending: {
    icon: Clock,
    tone: "text-default-secondary",
    label: "Planned step",
  },
  active: {
    icon: Loader2,
    tone: "text-accent",
    label: "Querying",
    spin: true,
  },
  completed: { icon: Check, tone: "text-feedback-success-primary", label: "Found" },
  // Neutral, NOT green: the source answered and had nothing, which an analyst
  // must be able to tell from a source that produced findings.
  empty: { icon: Minus, tone: "text-default-secondary", label: "Nothing found" },
  degraded: { icon: AlertTriangle, tone: "text-feedback-warning-primary", label: "Partial" },
  error: { icon: X, tone: "text-feedback-error-primary", label: "Failed" },
  skipped: {
    icon: SkipForward,
    tone: "text-default-secondary",
    label: "Not reached",
  },
};

/**
 * Translatable message per detail code. The parameter names match the
 * `*_params` dict the backend sends, so a code and its placeholders travel
 * together and cannot drift apart silently.
 *
 * The codes in the backend's `UNTRANSLATABLE_DETAIL_CODES` have no message of
 * their own on purpose: their useful content is text a third party wrote — an
 * exception message, an MCP server's own error string — so they fall through
 * to the free-text fallback. Giving them a translated message would replace
 * the only diagnostic those cases have.
 */
/**
 * Human labels for the backend's `SUBJECT_TYPES`.
 *
 * `run.no_covering_source` interpolates the subject type into a TRANSLATED
 * sentence, so passing the raw identifier produced "Aucune source ne couvre un
 * sujet de type threat_actor" — a snake_case English token mid-sentence, which
 * is the same French-over-English mismatch this vocabulary was introduced to
 * remove, one level down. The badge above renders the same labels, so the two
 * cannot disagree.
 *
 * An unknown type falls back to the raw value: a new subject type should read
 * awkwardly, not disappear.
 */
const SUBJECT_TYPE_LABELS: Record<string, string> = {
  ip: "IP address",
  domain: "domain",
  domain_list: "domain list",
  url: "URL",
  hash: "file hash",
  cve: "CVE",
  threat_actor: "threat actor",
  malware: "malware",
  campaign: "campaign",
  unknown: "unknown",
};

/**
 * Render a detail in the reader's language, falling back to the backend's free
 * text where no code exists.
 *
 * The fallback is not a defensive nicety: three of the backend's failure paths
 * carry a message lifted from a third-party exception, which cannot be
 * translated, and losing it would cost the only diagnostic those cases have.
 */
/** Clauses a code adds for a count it carries only when it is not zero
 *  (`DETAIL_CODE_OPTIONAL_PARAMS` in the backend): one step can set seeds aside
 *  and be stopped by a rate limit, and each cause is said. @i18n-keys */
const DETAIL_CLAUSE_TABLE = {
  "source.passive_dns_seeds": {
    set_aside: "{set_aside} resolved only to a non-public address and set aside",
    not_read: "{not_read} not read: VirusTotal's rate limit",
    failed: "VirusTotal did not answer for {failed}",
  },
  "source.passive_dns_filtered": {
    shown: "only the {shown} most recent are shown",
  },
  "source.passive_dns_filtered_more": {
    shown: "only the {shown} most recent are shown",
  },
} as const;
const DETAIL_CLAUSES: Partial<Record<InvestigationDetailCode, Readonly<Record<string, string>>>> =
  DETAIL_CLAUSE_TABLE;

function useDetailText() {
  const { t_i18n } = useFormatter();
  return (
    code: InvestigationDetailCode | null,
    params: Record<string, string | number> | null,
    fallback: string | null,
  ): string | null => {
    // A run stored before a code gained a parameter has its own text.
    const message = detailMessage(code, params);
    if (!message) return fallback;
    // The one free variable in the whole table that names a thing rather than
    // a number, so it is the one that needs translating before interpolation.
    const resolved =
      params && typeof params.subject_type === "string"
        ? {
            ...params,
            subject_type: t_i18n(
              SUBJECT_TYPE_LABELS[params.subject_type] ?? params.subject_type,
            ),
          }
        : params;
    let text = t_i18n(message, resolved ?? undefined);
    for (const [name, clause] of Object.entries((code && DETAIL_CLAUSES[code]) ?? {})) {
      const count = params?.[name];
      if (typeof count === "number" && count > 0) {
        text = t_i18n("{first}; {second}", { first: text, second: t_i18n(clause, { [name]: count }) });
      }
    }
    return text;
  };
}

/** Human labels for the shared error taxonomy. */
const ERROR_LABELS: Record<InvestigationErrorType, string> = {
  timeout: "Timed out",
  rate_limited: "Rate limited",
  not_found: "Not found",
  auth_error: "Access denied",
  unreachable: "Unreachable",
  invalid_response: "Unusable response",
};

function StepRow({ step }: { step: InvestigationStep }) {
  const { t_i18n } = useFormatter();
  const detailText = useDetailText();
  const style = STEP_STYLES[step.step_status] ?? STEP_STYLES.pending;
  const detail = detailText(
    step.detail_code,
    step.detail_params,
    step.status_detail,
  );
  const Icon = style.icon;
  const isLead = !!step.lead_from_step_id;

  return (
    <li className="flex items-start gap-2.5 px-3 py-2">
      <Icon
        className={cn(
          "mt-0.5 h-3.5 w-3.5 shrink-0",
          style.tone,
          style.spin && "animate-spin",
        )}
        aria-label={t_i18n(style.label)}
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="content-compact font-medium text-default-primary">
            {step.source_name}
          </span>
          {isLead && (
            <Tip
              content={t_i18n(
                "Discovered while reading another source, not part of the initial plan",
              )}
            >
              <Badge variant="outline" className="px-1.5 py-0 text-[9px]">
                {t_i18n("lead")}
              </Badge>
            </Tip>
          )}
          {step.error_type && (
            <Badge variant="destructive" className="px-1.5 py-0 text-[9px]">
              {t_i18n(ERROR_LABELS[step.error_type] ?? step.error_type)}
            </Badge>
          )}
          {step.step_status === "degraded" && !step.error_type && (
            <Badge variant="warning" className="px-1.5 py-0 text-[9px]">
              {t_i18n("Partial")}
            </Badge>
          )}
          {step.findings_count > 0 && (
            <span className="content-caption text-default-secondary">
              {step.findings_count === 1
                ? t_i18n("1 finding")
                : t_i18n("{count} findings", { count: step.findings_count })}
            </span>
          )}
          {/* Rendered only when the payload carries one — there is no default. */}
          {step.confidence !== null && (
            <span className="content-caption text-default-secondary">
              {t_i18n("confidence {pct}%", {
                pct: Math.round(step.confidence * 100),
              })}
            </span>
          )}
        </div>
        {step.query && (
          <div className="mt-0.5 truncate font-mono-plex content-caption text-default-secondary/80">
            {step.query}
          </div>
        )}
        {detail && (
          <div className="mt-0.5 content-caption text-default-secondary">
            {detail}
          </div>
        )}
        {/* A source that WROTE renders its prose with the citations placed in
            it; one that merely cites documents renders the list. Never both:
            `CitedAnswer` already ends with the numbered key, and a second
            list underneath would show every source twice. */}
        {step.answer ? (
          <CitedAnswer
            answer={step.answer}
            evidence={step.evidence}
            className="mt-1.5"
          />
        ) : (
          <StepEvidence evidence={step.evidence} />
        )}
      </div>
    </li>
  );
}

/**
 * What a step cited, under the step that cited it.
 *
 * Rendered here rather than in a footer list because a citation only means
 * something next to the source that produced it: "NVD · CVE-2024-3400" read
 * on its own does not say which of a run's twelve sources vouched for it.
 *
 * Only `url` evidence becomes a link. A knowledge-base document and an MCP
 * tool result carry no address — a KB may be shared with the agent and not
 * with the reader — so they render as text. Making them look clickable would
 * promise a destination that does not exist, which is the same overstatement
 * the step statuses are built to avoid.
 */
function StepEvidence({ evidence }: { evidence: InvestigationEvidence[] }) {
  const { t_i18n } = useFormatter();
  if (!evidence?.length) return null;
  return (
    <ul className="mt-1 space-y-0.5">
      {evidence.map((item, i) => (
        <li
          key={`${item.kind}-${item.label}-${i}`}
          className="flex items-start gap-1 content-caption text-default-secondary/80"
        >
          <span className="mt-[3px] h-1 w-1 shrink-0 rounded-full bg-[var(--text-default-secondary)]" />
          {item.kind === "url" ? (
            <EvidencePage label={item.label} href={item.href} />
          ) : (
            <span className="truncate">
              {item.label}
              <span className="ml-1 text-default-secondary">
                {item.kind === "document"
                  ? t_i18n("(knowledge base)")
                  : t_i18n("(tool result)")}
              </span>
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

/** A page a step read: linked only when its address is a web one (`safeHttpUrl`), else named. */
function EvidencePage({ label, href }: { label: string; href?: string | null }) {
  const safe = safeHttpUrl(href);
  if (!safe) return <span className="truncate">{label}</span>;
  return (
    <a
      href={safe}
      target="_blank"
      rel="noopener noreferrer"
      className="truncate underline decoration-dotted underline-offset-2 hover:text-default-primary"
    >
      {label}
    </a>
  );
}

/** The collapsed one-liner: `12 sources · 9 found · 2 failed · 1 not reached`. */
function summaryParts(
  run: Investigation,
  t_i18n: (id: string, values?: Record<string, string | number>) => string,
): string[] {
  const by = run.summary.by_status;
  const parts: string[] = [
    run.summary.total === 1
      ? t_i18n("1 source")
      : t_i18n("{count} sources", { count: run.summary.total }),
  ];
  // Ordered by what an analyst checks first: problems before successes.
  if (by.error) parts.push(t_i18n("{count} failed", { count: by.error }));
  if (by.degraded)
    parts.push(t_i18n("{count} partial", { count: by.degraded }));
  if (by.skipped)
    parts.push(t_i18n("{count} not reached", { count: by.skipped }));
  if (by.empty) parts.push(t_i18n("{count} empty", { count: by.empty }));
  if (by.completed)
    parts.push(t_i18n("{count} found", { count: by.completed }));
  return parts;
}

export function InvestigationBlock({
  investigationId,
}: {
  investigationId: string;
}) {
  const { t_i18n } = useFormatter();
  const detailText = useDetailText();
  const {
    data: run,
    isLoading,
    isError,
    stalled,
  } = useInvestigation(investigationId);
  // Auto-expanded while the work is happening, and kept open once it ends in
  // front of the reader: the report they watched being planned is what they
  // came for. A run met already finished (history) stays collapsed, so a
  // conversation of several investigations does not open every one of them.
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const liveNow = run?.status === "planning" || run?.status === "running";
  const runId = run?.id;
  useEffect(() => {
    if (liveNow) markSeenLive(runId);
  }, [liveNow, runId]);
  // Read from the tab's memory, not this component's: the block that shows
  // the finished run is a new one (the saved message replaces the streamed).
  const sawLive = liveNow || wasSeenLive(runId);
  // Re-read once the report wait is over, so a report that never came stops
  // reading "writing" without waiting for some other render.
  const [, setTick] = useState(0);
  const completedAt = run?.completed_at;
  useEffect(() => {
    if (!completedAt) return;
    const left = Date.parse(completedAt) + REPORT_WAIT_MS - Date.now();
    if (!(left > 0)) return;
    const timer = window.setTimeout(() => setTick((n) => n + 1), left + 50);
    return () => window.clearTimeout(timer);
  }, [completedAt]);

  if (isLoading) {
    return (
      <div className="my-2 flex items-center gap-2 rounded-sm border border-accent/25 layer-1 bg-elevation-default px-3 py-2.5">
        <Spinner size="sm" tone="inherit" className="text-accent" />
        <span className="content-compact text-default-secondary">
          {t_i18n("Loading investigation…")}
        </span>
      </div>
    );
  }

  if (isError || !run) {
    return <UnavailableCard message={t_i18n("This investigation is no longer available.")} />;
  }

  // A run whose status still says "running" but which has stopped reporting
  // progress. Saying "Investigating" for it would be the one thing the block
  // must never do — claim work is happening when nothing is.
  const isRunning =
    (run.status === "planning" || run.status === "running") && !stalled;
  const endReason = detailText(
    run.end_reason_code,
    run.end_reason_params,
    run.end_reason,
  );
  const plan = run.goal_plan ?? null;
  const awaiting = awaitingReport(run);
  const open = manualOpen ?? (isRunning || sawLive || awaiting);
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div className="my-2 overflow-hidden rounded-sm border border-accent/25 layer-1 bg-elevation-default">
      <button
        type="button"
        onClick={() => setManualOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-start gap-2 px-3 py-2.5 text-left transition-colors hover:bg-elevation-highlight"
      >
        <Chevron className="mt-0.5 h-3.5 w-3.5 shrink-0 text-default-secondary" />
        <Search className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="truncate content-compact font-semibold text-default-primary">
              {run.subject_value}
            </span>
            <Badge variant="outline" className="px-1.5 py-0 text-[9px]">
              {t_i18n(SUBJECT_TYPE_LABELS[run.subject_type] ?? run.subject_type)}
            </Badge>
            {isRunning && (
              <Badge variant="cyan" className="px-1.5 py-0 text-[9px]">
                {t_i18n("Investigating")}
              </Badge>
            )}
            {stalled && (
              <Badge variant="warning" className="px-1.5 py-0 text-[9px]">
                {t_i18n("No longer updating")}
              </Badge>
            )}
            {run.status === "cancelled" && (
              <Badge variant="warning" className="px-1.5 py-0 text-[9px]">
                {t_i18n("Cancelled")}
              </Badge>
            )}
            {run.status === "aborted" && (
              <Badge variant="destructive" className="px-1.5 py-0 text-[9px]">
                {t_i18n("Aborted")}
              </Badge>
            )}
          </div>
          <div className="mt-0.5 content-caption text-default-secondary">
            {summaryParts(run, t_i18n).join(" · ")}
            {/* Measured wall clock only — null while the run is unfinished. */}
            {run.duration_seconds !== null &&
              ` · ${formatDurationShort(run.duration_seconds)}`}
          </div>
        </div>
      </button>

      {open && plan && (
        <>
          <GoalPlanView
            run={run}
            plan={plan}
            live={isRunning}
            awaitingReport={awaiting}
            coverage={summaryParts(run, t_i18n).join(" · ")}
            renderStep={(step) => <StepRow step={step} />}
          />
          {!isRunning && endReason && (
            <div className="border-t border-elevation-subtle-soft bg-elevation-highlight px-3 py-2 content-caption text-default-secondary">
              {endReason}
            </div>
          )}
          <ContinuationPanel
            investigationId={investigationId}
            conversationId={run.conversation_id}
            enabled={!isRunning}
          />
        </>
      )}

      {open && !plan && (
        <>
          {run.steps.length > 0 ? (
            <ul className="divide-y divide-[var(--border-elevation-subtle)] border-t border-elevation-subtle-soft">
              {run.steps.map((step) => (
                <StepRow
                  key={step.step_id ?? `${step.position}-${step.source_name}`}
                  step={step}
                />
              ))}
            </ul>
          ) : (
            <div className="border-t border-elevation-subtle-soft px-3 py-2.5 content-caption text-default-secondary">
              {t_i18n("No source was queried.")}
            </div>
          )}

          {/* Why the run stopped. Shown for every terminal run, not just the
              unhappy ones: "budget spent" and "all planned sources queried"
              are different investigations and the difference matters. */}
          {!isRunning && endReason && (
            <div className="border-t border-elevation-subtle-soft bg-elevation-highlight px-3 py-2 content-caption text-default-secondary">
              {endReason}
              {run.iterations_used > 0 &&
                ` · ${t_i18n("{used}/{max} iterations", {
                  used: run.iterations_used,
                  max: run.max_iterations,
                })}`}
              {/* A count, not a suffix parsed out of the reason sentence.
                  Rendered here rather than hidden because leads found and
                  discarded are a gap in coverage, and the whole point of this
                  block is that gaps are visible. */}
              {run.leads_dropped > 0 &&
                ` · ${t_i18n("{count, plural, one {# lead} other {# leads}} dropped at the plan cap", {
                  count: run.leads_dropped,
                })}`}
            </div>
          )}

          {/* Gated on the server-computed flag, never on a count derived here,
              so this line is unreachable while any step is empty, degraded,
              errored, skipped or unfinished. */}
          {!isRunning && run.summary.all_succeeded && (
            <div className="flex items-center gap-1.5 border-t border-elevation-subtle-soft px-3 py-2">
              <Check className="h-3 w-3 text-feedback-success-primary" />
              <span className="content-caption font-medium text-feedback-success-primary">
                {t_i18n("Every source answered with findings")}
              </span>
            </div>
          )}
          {!isRunning &&
            !run.summary.all_succeeded &&
            run.summary.total > 0 && (
              <div className="flex items-center gap-1.5 border-t border-elevation-subtle-soft px-3 py-2">
                <AlertTriangle className="h-3 w-3 text-feedback-warning-primary" />
                <span className="content-caption text-default-secondary">
                  {t_i18n(
                    "Coverage is incomplete: read the per-source outcomes above before relying on this.",
                  )}
                </span>
              </div>
            )}

          {/* What is left of the plan, and what continuing would cost.
              Last in the block on purpose: it is a decision, and a decision
              belongs after the evidence it rests on. Renders nothing at all
              when the run queried its whole plan — an empty panel there would
              turn "complete" into a question. */}
          <ContinuationPanel
            investigationId={investigationId}
            conversationId={run.conversation_id}
            enabled={!isRunning}
          />
        </>
      )}
    </div>
  );
}
