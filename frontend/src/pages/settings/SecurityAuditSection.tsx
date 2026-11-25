// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ArrowRight,
  BellRing,
  Bot,
  BrainCircuit,
  Cable,
  ChevronDown,
  CircleCheck,
  CircleDot,
  Eye,
  EyeOff,
  Globe,
  History,
  Info,
  KeyRound,
  Layers,
  Lightbulb,
  ListFilter,
  Lock,
  Network,
  OctagonAlert,
  Plug2,
  Radio,
  RefreshCw,
  RotateCcw,
  SearchX,
  Server,
  Shield,
  ShieldAlert,
  ShieldCheck,
  ShieldOff,
  ShieldX,
  SlidersHorizontal,
  ToggleRight,
  TriangleAlert,
  Users,
  Workflow,
  Wrench,
  X,
  Zap,
  type LucideIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { Label } from "@/components/ui/label";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { SearchInput } from "@/components/ui/search-input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsGroupTitle } from "@/components/ui/settings-form";
import { Skeleton } from "@/components/ui/skeleton";
import { StatCard } from "@/components/ui/stat-card";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useDismissSecurityAuditFinding,
  useRestoreSecurityAuditFinding,
  useSecurityAudit,
} from "@/hooks/api/useSecurityAuditApi";
import { useSecurityOptions } from "@/hooks/api/useSettingsApi";
import { useAuth } from "@/hooks/useAuth";
import { useTick } from "@/hooks/useTick";
import { useFormatter } from "@/i18n";
import { formatTimeAgoLine } from "@/lib/relative-time";
import { sortByLabel } from "@/lib/select-options";
import { cn, toggleArrayItem } from "@/lib/utils";
import type {
  SecurityAuditFinding,
  SecurityAuditFindingDismissal,
  SecurityAuditFindingReopening,
  SecurityAuditReport,
  SecurityControlStatus,
  SecurityOption,
} from "@/types";

import {
  DISMISSAL_REASON_MAX_CHARS,
  NO_CONTROL_FILTERS,
  NO_FILTERS,
  canDismiss,
  categoryLabel,
  controlCategories,
  controlStateLabel,
  controlStateTone,
  filterControls,
  filterFindings,
  findingKeys,
  findingsByControl,
  focusedFindings,
  groupFindings,
  hardenedPhrase,
  hasActiveControlFilters,
  hasActiveFilters,
  headline,
  isDismissed,
  passRate,
  percent,
  scoreLabel,
  scoreTone,
  severityHint,
  severityLabel,
  severityTone,
  sortControls,
  splitDismissed,
  summarizeCategories,
  tally,
  tallyControls,
  worstLinkedFinding,
  type CategorySummary,
  type ControlFilters,
  type ControlTally,
  type FindingFilters,
  type FindingGroup,
  type GroupBy,
  type KnownControlState,
  type KnownSeverity,
  type Tally,
  type Tone,
} from "./security-audit";
import {
  controlOption,
  controlOptionKey,
  linkedOptionKey,
  lockMessage,
  optionLabel,
  optionLocation,
  optionSourceMessage,
} from "./security-options";
import { ConfigCode, ConfigText } from "./ConfigText";

// ---------------------------------------------------------------------------
// Settings > Access and security > Security audit: the report of
// `GET /security-audit` as a posture score, severity and category statistics
// and the findings themselves. Admin-only, like the endpoint.
// ---------------------------------------------------------------------------

interface ToneClasses {
  text: string;
  soft: string;
  border: string;
  fill: string;
  dot: string;
}

const TONE_CLASSES: Record<Tone, ToneClasses> = {
  danger: { text: "text-feedback-error-primary light:text-feedback-error-primary", soft: "bg-feedback-error-primary/10", border: "border-feedback-error-primary/30", fill: "fill-feedback-error-primary", dot: "bg-feedback-error-primary" },
  warning: { text: "text-feedback-warning-primary light:text-feedback-warning-primary", soft: "bg-feedback-alert-primary/10", border: "border-feedback-alert-primary/30", fill: "fill-feedback-alert-primary", dot: "bg-feedback-alert-primary" },
  info: { text: "text-feedback-info-primary light:text-feedback-info-primary", soft: "bg-feedback-info-primary/10", border: "border-feedback-info-primary/30", fill: "fill-feedback-info-primary", dot: "bg-feedback-info-primary" },
  success: { text: "text-feedback-success-primary light:text-feedback-success-primary", soft: "bg-feedback-success-primary/10", border: "border-feedback-success-primary/30", fill: "fill-feedback-success-primary", dot: "bg-feedback-success-primary" },
  neutral: { text: "text-default-secondary", soft: "bg-elevation-hover", border: "border-elevation-subtle", fill: "fill-[var(--text-default-secondary)]", dot: "bg-[var(--text-default-secondary)]" },
};

/** A table without inherited keys: a category or a severity named `constructor` finds nothing in it. */
function iconTable<K extends string>(entries: Record<K, LucideIcon>): Readonly<Record<string, LucideIcon | undefined>> {
  return Object.freeze(Object.assign(Object.create(null) as Record<string, LucideIcon>, entries));
}

const SEVERITY_ICONS = iconTable<KnownSeverity>({
  critical: OctagonAlert,
  warning: TriangleAlert,
  info: Info,
  pass: CircleCheck,
});

const SCORE_ICONS = iconTable({
  at_risk: ShieldX,
  needs_attention: ShieldAlert,
  healthy: ShieldCheck,
});

const CATEGORY_ICONS = iconTable({
  access: Users,
  agents: Bot,
  assignments: Zap,
  authentication: KeyRound,
  channels: Radio,
  encryption: Lock,
  flows: Workflow,
  infrastructure: Server,
  integrations: Plug2,
  llm: BrainCircuit,
  mcp: Cable,
  network: Network,
  tools: Wrench,
  web: Globe,
});

const CONTROL_STATE_ICONS = iconTable<KnownControlState>({
  enforced: ShieldCheck,
  monitoring: Eye,
  off: ShieldOff,
});

const ALL_CATEGORIES = "__all__";
const VIEW_PARAM = "view";

type AuditView = "findings" | "controls";

const toneOf = (severity: string) => TONE_CLASSES[severityTone(severity)];

/** The Findings | Controls view, kept in the URL (`?view=controls`) so a link opens it. */
function useAuditView(): [AuditView, (view: AuditView) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const view: AuditView = searchParams.get(VIEW_PARAM) === "controls" ? "controls" : "findings";
  const setView = useCallback(
    (next: AuditView) =>
      setSearchParams(
        (previous) => {
          const params = new URLSearchParams(previous);
          if (next === "controls") params.set(VIEW_PARAM, next);
          else params.delete(VIEW_PARAM);
          return params;
        },
        { replace: true },
      ),
    [setSearchParams],
  );
  return [view, setView];
}

export function SecurityAuditSection() {
  const { isAdmin } = useAuth();
  const audit = useSecurityAudit(isAdmin);
  // Only to link a finding or a control to the option it names.
  const securityOptions = useSecurityOptions(isAdmin);
  // A second click while a run is in flight joins it instead of starting another.
  const run = () => void audit.refetch({ cancelRefetch: false });

  if (!isAdmin) return null;
  if (audit.isPending) return <AuditSkeleton />;
  if (!audit.data) {
    return <AuditUnavailable error={audit.error} retrying={audit.isFetching} onRetry={run} />;
  }
  return (
    <AuditReport
      report={audit.data}
      receivedAt={audit.dataUpdatedAt}
      running={audit.isFetching}
      rerunError={audit.isRefetchError ? audit.error : null}
      onRerun={run}
      options={securityOptions.data?.options}
    />
  );
}

function AuditSkeleton() {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-6" role="status" aria-label={t_i18n("Running the security audit...")} data-testid="security-audit-loading">
      <Skeleton className="h-52 w-full rounded-sm" />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-24 rounded-sm" />
        ))}
      </div>
      <Skeleton className="h-44 w-full rounded-sm" />
      <Skeleton className="h-72 w-full rounded-sm" />
    </div>
  );
}

function AuditUnavailable({ error, retrying, onRetry }: { error: Error | null; retrying: boolean; onRetry: () => void }) {
  return (
    <div data-testid="security-audit-error">
      <EmptyState
        icon={ShieldAlert}
        title="The security audit could not run"
        description={error?.message || "The platform did not return a report."}
        actionLabel={retrying ? "Running..." : "Try again"}
        actionIcon={RefreshCw}
        actionVariant="default"
        onAction={onRetry}
      />
    </div>
  );
}

function AuditReport({
  report,
  receivedAt,
  running,
  rerunError,
  onRerun,
  options,
}: {
  report: SecurityAuditReport;
  /** When this browser received the report (ms): the relative time reads it, free of server clock skew. */
  receivedAt: number;
  running: boolean;
  rerunError: Error | null;
  onRerun: () => void;
  /** The security options, once read: a finding links to an option they do not lock. */
  options: SecurityOption[] | undefined;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  // A dismissed finding is out of every count, filter and group: it is listed on its own.
  const { active: findings, dismissed } = useMemo(() => splitDismissed(report.findings), [report.findings]);
  const totals = useMemo(() => tally(findings), [findings]);
  const categories = useMemo(() => summarizeCategories(findings), [findings]);
  const [filters, setFilters] = useState<FindingFilters>(NO_FILTERS);
  const [groupBy, setGroupBy] = useState<GroupBy>("severity");
  // A platform older reports no controls: the Findings view is all there is.
  const controls = report.controls;
  const controlTotals = useMemo(() => (controls ? tallyControls(controls) : null), [controls]);
  const [requestedView, setView] = useAuditView();
  const view: AuditView = controls ? requestedView : "findings";

  // The search also matches the labels the screen shows, which are translated.
  const labelsOf = useCallback(
    (finding: SecurityAuditFinding) => `${t_i18n(severityLabel(finding.severity))} ${t_i18n(categoryLabel(finding.category))}`,
    [t_i18n],
  );
  const visible = useMemo(() => filterFindings(findings, filters, labelsOf), [findings, filters, labelsOf]);
  const groups = useMemo(() => groupFindings(visible, groupBy), [visible, groupBy]);

  // ``?finding=<id>`` (repeatable): the findings a Security options entry
  // links to, highlighted and scrolled to when this run reports one of them.
  const [searchParams] = useSearchParams();
  const requestedKey = searchParams.getAll("finding").join("\n");
  const focused = useMemo(
    () => focusedFindings(report.findings, requestedKey ? requestedKey.split("\n") : []),
    [report.findings, requestedKey],
  );
  const firstFocused = report.findings.find((finding) => finding.id && focused.has(finding.id))?.id ?? null;
  useEffect(() => {
    if (!firstFocused) return;
    const id = CSS.escape(firstFocused);
    document
      .querySelector(
        `[data-testid="security-audit-finding"][data-finding-id="${id}"], [data-testid="security-audit-dismissed-finding"][data-finding-id="${id}"]`,
      )
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [firstFocused]);

  const selectCategory = (category: string) =>
    setFilters((current) => ({ ...current, category: current.category === category ? null : category }));

  const dismiss = useDismissSecurityAuditFinding();
  const restore = useRestoreSecurityAuditFinding();
  const [dismissing, setDismissing] = useState<SecurityAuditFinding | null>(null);
  const confirmDismiss = (reason: string) => {
    const finding = dismissing;
    if (!finding?.id) return;
    dismiss.mutate(
      { findingId: finding.id, reason: reason.trim() || null },
      {
        onSuccess: () => {
          setDismissing(null);
          toast({ variant: "success", title: t_i18n("Finding dismissed"), description: finding.title });
        },
        onError: (error) =>
          toast({ variant: "error", title: t_i18n("The finding could not be dismissed"), description: error.message }),
      },
    );
  };
  const restoreFinding = (finding: SecurityAuditFinding) => {
    if (!finding.id) return;
    restore.mutate(finding.id, {
      onSuccess: () => toast({ variant: "success", title: t_i18n("Finding restored"), description: finding.title }),
      onError: (error) =>
        toast({ variant: "error", title: t_i18n("The finding could not be restored"), description: error.message }),
    });
  };

  return (
    <div className="space-y-6" data-testid="security-audit">
      {rerunError && <RerunFailed error={rerunError} />}
      <ScoreHero
        report={report}
        receivedAt={receivedAt}
        totals={totals}
        dismissed={dismissed.length}
        controls={controlTotals}
        running={running}
        rerunFailed={rerunError !== null}
        onRerun={onRerun}
      />
      <SeverityStats totals={totals} />
      {controls && (
        <ViewSwitch view={view} onView={setView} findings={findings.length} controls={controls.length} />
      )}
      {view === "findings" && requestedKey !== "" && focused.size === 0 && (
        <p className="content-compact text-default-secondary" role="status" data-testid="security-audit-focus-absent">
          {t_i18n("This run reports no finding for that option: its check passed or does not apply.")}
        </p>
      )}
      {view === "controls" && controls ? (
        <ControlsPanel controls={controls} findings={report.findings} options={options} />
      ) : (
        <>
          {findings.length === 0 ? (
            dismissed.length > 0 ? (
              <EmptyState icon={ShieldCheck} title="No finding left to review" description="Every finding this run reports was dismissed." />
            ) : (
              <EmptyState icon={ShieldCheck} title="No findings reported" description="The audit ran and reported no finding." />
            )
          ) : (
            <>
              <CategoryBreakdown categories={categories} active={filters.category} onSelect={selectCategory} />
              <FindingsPanel
                total={findings.length}
                totals={totals}
                categories={categories}
                visible={visible}
                groups={groups}
                groupBy={groupBy}
                onGroupBy={setGroupBy}
                filters={filters}
                onFilters={setFilters}
                focused={focused}
                options={options}
                onDismiss={setDismissing}
              />
            </>
          )}
          {dismissed.length > 0 && (
            <DismissedFindingsPanel
              findings={dismissed}
              focused={focused}
              restoringId={restore.isPending ? restore.variables : null}
              onRestore={restoreFinding}
            />
          )}
        </>
      )}
      {dismissing && (
        <DismissFindingDialog
          key={dismissing.id}
          finding={dismissing}
          pending={dismiss.isPending}
          onCancel={() => setDismissing(null)}
          onConfirm={confirmDismiss}
        />
      )}
    </div>
  );
}

function RerunFailed({ error }: { error: Error }) {
  const { t_i18n } = useFormatter();
  return (
    <div
      role="alert"
      data-testid="security-audit-rerun-error"
      className="flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 p-3 content-compact text-feedback-warning-tertiary light:text-feedback-warning-tertiary"
    >
      <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <p>
        {error.message
          ? t_i18n("The audit could not run again ({message}). The report below is from the previous run.", { message: error.message })
          : t_i18n("The audit could not run again. The report below is from the previous run.")}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Score
// ---------------------------------------------------------------------------

function ScoreHero({
  report,
  receivedAt,
  totals,
  dismissed,
  controls,
  running,
  rerunFailed,
  onRerun,
}: {
  report: SecurityAuditReport;
  receivedAt: number;
  totals: Tally;
  /** How many findings an administrator dismissed: none of them is in `totals` or the score. */
  dismissed: number;
  /** `null` for a platform that reports no controls. */
  controls: ControlTally | null;
  running: boolean;
  rerunFailed: boolean;
  onRerun: () => void;
}) {
  const { t_i18n } = useFormatter();
  const tone = TONE_CLASSES[scoreTone(report.overall_score)];
  const ScoreIcon = SCORE_ICONS[report.overall_score] ?? Shield;
  const head = headline(totals);
  const headlineText =
    head.kind === "critical"
      ? t_i18n("{count, plural, one {# critical finding needs immediate action.} other {# critical findings need immediate action.}}", { count: head.count })
      : head.kind === "warning"
        ? t_i18n("{count, plural, one {# warning to review, no critical finding.} other {# warnings to review, no critical finding.}}", { count: head.count })
        : head.kind === "review"
          ? t_i18n("{count, plural, one {# finding to review.} other {# findings to review.}}", { count: head.count })
          : t_i18n("No critical or warning finding.");

  return (
    <Card
      data-testid="security-audit-score"
      data-score={report.overall_score}
      className={cn("layer-1 bg-elevation-default backdrop-blur-sm", tone.border)}
    >
      <CardHeader className="flex flex-col gap-3 space-y-0 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1.5">
          <CardTitle className="flex items-center gap-2">
            <Shield className="h-4 w-4 text-accent" />
            {t_i18n("Security audit")}
          </CardTitle>
          <CardDescription>
            {t_i18n("An automated review of this platform's security configuration. Every run checks the live settings again.")}
          </CardDescription>
        </div>
        <RerunControl
          timestamp={report.timestamp}
          receivedAt={receivedAt}
          running={running}
          rerunFailed={rerunFailed}
          onRerun={onRerun}
        />
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="flex flex-col gap-5 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-4">
            <div className={cn("flex h-14 w-14 shrink-0 items-center justify-center rounded-sm", tone.soft)}>
              <ScoreIcon className={cn("h-7 w-7", tone.text)} />
            </div>
            <div className="min-w-0">
              <p className="content-caption font-medium uppercase tracking-wider text-default-secondary">{t_i18n("Security posture")}</p>
              <p className={cn("font-sans-geologica text-2xl font-bold leading-tight", tone.text)} data-testid="security-audit-score-label">
                {t_i18n(scoreLabel(report.overall_score))}
              </p>
              <p className="mt-0.5 content-base text-default-secondary" data-testid="security-audit-headline">
                {headlineText}
              </p>
              {controls && controls.total > 0 && (
                <p
                  className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 content-compact text-default-secondary"
                  data-testid="security-audit-controls-enforced"
                >
                  <ShieldCheck className={cn("h-3.5 w-3.5", TONE_CLASSES.success.text)} />
                  <span>
                    {t_i18n(
                      "{total, plural, one {{enforced} of # control enforced} other {{enforced} of # controls enforced}}",
                      { enforced: controls.enforced, total: controls.total },
                    )}
                  </span>
                  {controls.monitoring > 0 && (
                    <>
                      <span aria-hidden="true">·</span>
                      <span>{t_i18n("{count} monitoring", { count: controls.monitoring })}</span>
                    </>
                  )}
                </p>
              )}
              {dismissed > 0 && (
                <p
                  className="mt-1.5 flex items-center gap-2 content-compact text-default-secondary"
                  data-testid="security-audit-dismissed-count"
                >
                  <EyeOff className="h-3.5 w-3.5" />
                  {t_i18n(
                    "{count, plural, one {# dismissed finding is not counted} other {# dismissed findings are not counted}}",
                    { count: dismissed },
                  )}
                </p>
              )}
            </div>
          </div>
          <PassRate totals={totals} />
        </div>
        <SeverityMix totals={totals} />
      </CardContent>
    </Card>
  );
}

function RerunControl({
  timestamp,
  receivedAt,
  running,
  rerunFailed,
  onRerun,
}: {
  timestamp: string;
  receivedAt: number;
  running: boolean;
  /** The last re-run failed: the alert above says so, the status line does not claim it completed. */
  rerunFailed: boolean;
  onRerun: () => void;
}) {
  const { t_i18n, intl } = useFormatter();
  useTick();
  const ranText = formatTimeAgoLine(intl, receivedAt, {
    line: (time) => t_i18n("Ran {time}", { time }),
    justNow: () => t_i18n("Ran just now"),
    fallback: timestamp,
  });
  const ranAt = Date.parse(timestamp);
  const exactTime = Number.isNaN(ranAt) ? timestamp : intl.formatDate(ranAt, { dateStyle: "medium", timeStyle: "medium" });

  return (
    <div className="flex shrink-0 items-center gap-3">
      <Tip content={exactTime}>
        <span
          tabIndex={0}
          className="flex items-center gap-1.5 rounded-sm content-compact text-default-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          data-testid="security-audit-last-run"
        >
          <History className="h-3.5 w-3.5" />
          <time dateTime={timestamp}>{ranText}</time>
        </span>
      </Tip>
      <Button
        variant="outline"
        size="sm"
        className="gap-1.5"
        onClick={onRerun}
        disabled={running}
        aria-busy={running}
        data-testid="security-audit-rerun"
      >
        <RefreshCw className={cn("h-3.5 w-3.5", running && "animate-spin")} />
        {running ? t_i18n("Running...") : t_i18n("Re-run")}
      </Button>
      <span className="sr-only" role="status">
        {running ? t_i18n("Running the security audit...") : rerunFailed ? "" : t_i18n("Security audit complete.")}
      </span>
    </div>
  );
}

function PassRate({ totals }: { totals: Tally }) {
  const { t_i18n, intl } = useFormatter();
  const rate = passRate(totals);
  return (
    <div className="shrink-0 md:text-right" data-testid="security-audit-pass-rate">
      <div className="flex items-center gap-1 content-caption font-medium uppercase tracking-wider text-default-secondary md:justify-end">
        {t_i18n("Pass rate")}
        <Tip content={t_i18n("Share of the checks that passed. Informational findings neither pass nor fail, so they are not counted.")}>
          <button
            type="button"
            className="rounded-sm hover:text-default-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            aria-label={t_i18n("How the pass rate is computed")}
          >
            <Info className="h-3 w-3" />
          </button>
        </Tip>
      </div>
      <p className="font-sans-geologica text-3xl font-bold" data-testid="security-audit-pass-rate-value">
        {rate === null ? "-" : intl.formatNumber(percent(rate) / 100, { style: "percent" })}
      </p>
      <p className="content-compact text-default-secondary">
        {rate === null
          ? t_i18n("No check to pass or fail")
          : t_i18n("{checked, plural, one {{passed} of # check passed} other {{passed} of # checks passed}}", {
              passed: totals.passed,
              checked: totals.checked,
            })}
      </p>
    </div>
  );
}

/** The report's findings as one bar, most urgent severity first. */
function SeverityMix({ totals }: { totals: Tally }) {
  const { t_i18n, intl } = useFormatter();
  if (totals.total === 0) return null;
  const segments = totals.severities.filter((entry) => entry.count > 0);
  const starts = segments.map((_, index) => segments.slice(0, index).reduce((sum, entry) => sum + entry.count, 0));
  const description = intl.formatList(
    segments.map((entry) =>
      t_i18n("{severity}: {count}", { severity: t_i18n(severityLabel(entry.severity)), count: entry.count }),
    ),
    { type: "conjunction" },
  );
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-elevation-hover" data-testid="security-audit-mix">
      <svg
        viewBox={`0 0 ${totals.total} 1`}
        preserveAspectRatio="none"
        className="block h-full w-full"
        role="img"
        aria-label={description}
      >
        {segments.map((entry, index) => (
          <rect
            key={entry.severity}
            x={starts[index]}
            y={0}
            width={entry.count}
            height={1}
            className={toneOf(entry.severity).fill}
          />
        ))}
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Statistics
// ---------------------------------------------------------------------------

function SeverityStats({ totals }: { totals: Tally }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(10rem,1fr))] gap-4" data-testid="security-audit-stats">
      {totals.severities.map(({ severity, count }) => {
        const tone = severityTone(severity);
        const alarming = count > 0 && (tone === "danger" || tone === "warning");
        return (
          <div key={severity} data-testid="security-audit-stat" data-severity={severity} data-count={count}>
            <StatCard
              icon={SEVERITY_ICONS[severity] ?? CircleDot}
              label={severityLabel(severity)}
              value={count}
              subtitle={severityHint(severity)}
              iconClassName={TONE_CLASSES[tone].text}
              className={cn("h-full", alarming && cn(TONE_CLASSES[tone].border, TONE_CLASSES[tone].soft))}
            />
          </div>
        );
      })}
    </div>
  );
}

function SeverityPills({ counts }: { counts: Tally }) {
  const { t_i18n } = useFormatter();
  return (
    <span className="flex flex-wrap gap-1.5">
      {counts.severities
        .filter((entry) => entry.count > 0)
        .map(({ severity, count }) => {
          const tone = toneOf(severity);
          const Icon = SEVERITY_ICONS[severity] ?? CircleDot;
          return (
            <span
              key={severity}
              className={cn("inline-flex items-center gap-1 rounded-sm border border-transparent px-2 py-0.5 content-caption font-medium tabular-nums", tone.soft, tone.text)}
            >
              <Icon className="h-3 w-3" />
              {count}
              <span className="sr-only">{t_i18n(severityLabel(severity))}</span>
            </span>
          );
        })}
    </span>
  );
}

/** A pass rate as a bar; nothing drawn for a category with nothing to pass or fail. */
function PassMeter({ rate }: { rate: number | null }) {
  return (
    <span className="block h-1.5 w-full overflow-hidden rounded-full bg-elevation-hover">
      {rate !== null && (
        <svg viewBox="0 0 100 1" preserveAspectRatio="none" className="block h-full w-full">
          <rect x={0} y={0} width={rate * 100} height={1} className={TONE_CLASSES.success.fill} />
        </svg>
      )}
    </span>
  );
}

function CategoryBreakdown({
  categories,
  active,
  onSelect,
}: {
  categories: CategorySummary[];
  active: string | null;
  onSelect: (category: string) => void;
}) {
  const { t_i18n, intl } = useFormatter();
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">{t_i18n("By category")}</CardTitle>
        <CardDescription>{t_i18n("Select a category to show only its findings.")}</CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" data-testid="security-audit-categories">
          {categories.map((summary) => {
            const Icon = CATEGORY_ICONS[summary.category] ?? Layers;
            const worst = severityTone(summary.worst);
            const rate = passRate(summary);
            const selected = active === summary.category;
            return (
              <li key={summary.category}>
                <button
                  type="button"
                  aria-pressed={selected}
                  onClick={() => onSelect(summary.category)}
                  data-testid="security-audit-category"
                  data-category={summary.category}
                  className={cn(
                    "flex h-full w-full flex-col gap-3 rounded-sm border p-4 text-left transition-colors hover:bg-elevation-hover",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                    selected
                      ? "border-accent/60 bg-accent/5"
                      : worst === "danger" || worst === "warning"
                        ? TONE_CLASSES[worst].border
                        : "border-elevation-subtle-soft",
                  )}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-2 content-base font-medium text-default-primary">
                      <Icon className={cn("h-4 w-4 shrink-0", TONE_CLASSES[worst].text)} />
                      <span className="truncate">{t_i18n(categoryLabel(summary.category))}</span>
                    </span>
                    <span className="shrink-0 content-compact text-default-secondary">
                      {t_i18n("{count, plural, one {# finding} other {# findings}}", { count: summary.total })}
                    </span>
                  </span>
                  <SeverityPills counts={summary} />
                  <span className="block space-y-1">
                    <span className="flex items-center justify-between content-compact text-default-secondary">
                      <span>{t_i18n("Pass rate")}</span>
                      <span className="font-medium tabular-nums text-default-primary" data-testid="security-audit-category-pass-rate">
                        {rate === null ? "-" : intl.formatNumber(percent(rate) / 100, { style: "percent" })}
                      </span>
                    </span>
                    <PassMeter rate={rate} />
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Findings
// ---------------------------------------------------------------------------

function FindingsPanel({
  total,
  totals,
  categories,
  visible,
  groups,
  groupBy,
  onGroupBy,
  filters,
  onFilters,
  focused,
  options,
  onDismiss,
}: {
  total: number;
  totals: Tally;
  categories: CategorySummary[];
  visible: SecurityAuditFinding[];
  groups: FindingGroup[];
  groupBy: GroupBy;
  onGroupBy: (by: GroupBy) => void;
  filters: FindingFilters;
  onFilters: (update: (current: FindingFilters) => FindingFilters) => void;
  /** Ids a Security options link asked for (`?finding=`). */
  focused: ReadonlySet<string>;
  options: SecurityOption[] | undefined;
  onDismiss: (finding: SecurityAuditFinding) => void;
}) {
  const { t_i18n, locale } = useFormatter();
  const reported = categories.map((summary) => summary.category);
  // A filter kept across a re-run may name a category the new report no longer has.
  if (filters.category !== null && !reported.includes(filters.category)) reported.push(filters.category);
  const categoryOptions = sortByLabel(reported, (category) => t_i18n(categoryLabel(category)), locale);
  const clear = () => onFilters(() => NO_FILTERS);

  return (
    <Card data-testid="security-audit-findings">
      <CardHeader className="gap-4 space-y-0 pb-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CardTitle className="text-base">{t_i18n("Findings")}</CardTitle>
          <div className="flex items-center gap-2">
            <span className="content-compact text-default-secondary">{t_i18n("Group by")}</span>
            <div role="group" aria-label={t_i18n("Group findings by")} className="inline-flex items-center gap-1 rounded-sm bg-elevation-hover p-1">
              {(["severity", "category"] as const).map((by) => {
                const Icon = by === "severity" ? ListFilter : Layers;
                return (
                  <button
                    key={by}
                    type="button"
                    aria-pressed={groupBy === by}
                    onClick={() => onGroupBy(by)}
                    data-testid="security-audit-group-by"
                    data-group-by={by}
                    className={cn(
                      "flex items-center gap-1.5 rounded-sm px-3 py-1.5 content-compact font-medium transition-all",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
                      groupBy === by
                        ? "bg-accent/10 text-accent shadow-xs"
                        : "text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
                    )}
                  >
                    <Icon className="h-3.5 w-3.5" />
                    {by === "severity" ? t_i18n("Severity") : t_i18n("Category")}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <SearchInput
            placeholder="Search findings..."
            value={filters.query}
            onChange={(query) => onFilters((current) => ({ ...current, query }))}
            className="w-full"
            inputClassName="h-9"
          />
          <Select
            value={filters.category ?? ALL_CATEGORIES}
            onValueChange={(value) =>
              onFilters((current) => ({ ...current, category: value === ALL_CATEGORIES ? null : value }))
            }
          >
            <SelectTrigger className="h-9 w-full sm:w-56" aria-label={t_i18n("Filter by category")} data-testid="security-audit-category-filter">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_CATEGORIES}>{t_i18n("All categories")}</SelectItem>
              {categoryOptions.map((category) => (
                <SelectItem key={category} value={category}>
                  {t_i18n(categoryLabel(category))}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {hasActiveFilters(filters) && (
            <Button variant="ghost" size="sm" className="gap-1.5 self-start sm:ml-auto sm:self-auto" onClick={clear} data-testid="security-audit-clear-filters">
              <X className="h-3.5 w-3.5" />
              {t_i18n("Clear filters")}
            </Button>
          )}
        </div>
        <div role="group" aria-label={t_i18n("Filter by severity")} className="flex flex-wrap items-center gap-1.5">
          {totals.severities.map(({ severity, count }) => {
            const pressed = filters.severities.includes(severity);
            const tone = toneOf(severity);
            const Icon = SEVERITY_ICONS[severity] ?? CircleDot;
            return (
              <button
                key={severity}
                type="button"
                aria-pressed={pressed}
                disabled={count === 0 && !pressed}
                onClick={() =>
                  onFilters((current) => ({ ...current, severities: toggleArrayItem([...current.severities], severity) }))
                }
                data-testid="security-audit-severity-filter"
                data-severity={severity}
                className={cn(
                  "inline-flex h-9 items-center gap-1.5 rounded-sm border px-3 content-compact font-medium transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:opacity-40",
                  pressed
                    ? cn(tone.border, tone.soft, tone.text)
                    : "border-elevation-subtle-soft text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
                )}
              >
                <Icon className={cn("h-3.5 w-3.5", tone.text)} />
                {t_i18n(severityLabel(severity))}
                <span className="rounded-sm bg-elevation-hover px-1.5 tabular-nums">{count}</span>
              </button>
            );
          })}
        </div>
        <p className="content-compact text-default-secondary" aria-live="polite" data-testid="security-audit-result-count">
          {t_i18n("{total, plural, one {Showing {shown} of # finding} other {Showing {shown} of # findings}}", {
            shown: visible.length,
            total,
          })}
        </p>
      </CardHeader>
      <CardContent className="space-y-6">
        {visible.length === 0 ? (
          <EmptyState
            variant="inset"
            icon={SearchX}
            title="No matching findings"
            description="No finding matches the current filters."
            actionLabel="Clear filters"
            actionIcon={X}
            onAction={clear}
          />
        ) : (
          groups.map((group) => (
            <FindingGroupSection
              key={group.key}
              group={group}
              groupBy={groupBy}
              focused={focused}
              options={options}
              onDismiss={onDismiss}
            />
          ))
        )}
      </CardContent>
    </Card>
  );
}

function FindingGroupSection({
  group,
  groupBy,
  focused,
  options,
  onDismiss,
}: {
  group: FindingGroup;
  groupBy: GroupBy;
  focused: ReadonlySet<string>;
  options: SecurityOption[] | undefined;
  onDismiss: (finding: SecurityAuditFinding) => void;
}) {
  const { t_i18n } = useFormatter();
  const headingId = useId();
  const bySeverity = groupBy === "severity";
  const Icon = (bySeverity ? SEVERITY_ICONS[group.key] : CATEGORY_ICONS[group.key]) ?? (bySeverity ? CircleDot : Layers);
  const keys = findingKeys(group.findings);
  return (
    <section aria-labelledby={headingId} data-testid="security-audit-group" data-group={group.key}>
      <SettingsGroupTitle
        id={headingId}
        className="mb-3"
        icon={Icon}
        iconClassName={bySeverity ? toneOf(group.key).text : undefined}
      >
        {t_i18n(bySeverity ? severityLabel(group.key) : categoryLabel(group.key))}
        <span className="rounded-sm bg-elevation-hover px-1.5 content-compact font-medium tabular-nums text-default-secondary">{group.findings.length}</span>
      </SettingsGroupTitle>
      <ul className="space-y-3">
        {group.findings.map((finding, index) => (
          <FindingItem
            key={keys[index]}
            finding={finding}
            showCategory={bySeverity}
            focused={!!finding.id && focused.has(finding.id)}
            linkedOption={linkedOptionKey(finding.id, options)}
            onDismiss={canDismiss(finding) ? () => onDismiss(finding) : null}
          />
        ))}
      </ul>
    </section>
  );
}

function FindingItem({
  finding,
  showCategory,
  focused,
  linkedOption,
  onDismiss,
}: {
  finding: SecurityAuditFinding;
  showCategory: boolean;
  /** Asked for by a Security options link: set apart from the others. */
  focused: boolean;
  /** The option whose field the recommendation links to, or `null`. */
  linkedOption: string | null;
  /** `null` for a finding that cannot be dismissed: a pass, or one without a stable id. */
  onDismiss: (() => void) | null;
}) {
  const { t_i18n } = useFormatter();
  const tone = toneOf(finding.severity);
  const Icon = SEVERITY_ICONS[finding.severity] ?? CircleDot;
  const CategoryIcon = CATEGORY_ICONS[finding.category] ?? Layers;
  return (
    <li
      data-testid="security-audit-finding"
      data-finding-id={finding.id ?? undefined}
      data-severity={finding.severity}
      data-category={finding.category}
      data-focused={focused ? "true" : undefined}
      className={cn(
        "rounded-sm border layer-1 bg-elevation-default p-4",
        finding.severity === "pass" ? "border-elevation-subtle-soft" : tone.border,
        focused && "ring-2 ring-accent/60",
      )}
    >
      <div className="flex items-start gap-3">
        <span className={cn("mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-sm", tone.soft)}>
          <Icon className={cn("h-4 w-4", tone.text)} />
        </span>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h5 className="content-base font-medium text-default-primary">{finding.title}</h5>
            <Badge variant="outline" className={cn("content-caption font-medium", tone.text, tone.border)}>
              {t_i18n(severityLabel(finding.severity))}
            </Badge>
            {showCategory && (
              <Badge variant="outline" className="gap-1 content-caption font-medium text-default-secondary">
                <CategoryIcon className="h-3 w-3" />
                {t_i18n(categoryLabel(finding.category))}
              </Badge>
            )}
            {onDismiss && (
              <Tip content={t_i18n("Accept this finding: it leaves the counts and the score, and is never notified.")}>
                <Button
                  variant="ghost"
                  size="sm"
                  className="ml-auto h-7 gap-1.5 px-2 content-compact text-default-secondary hover:text-default-primary"
                  onClick={onDismiss}
                  data-testid="security-audit-dismiss"
                >
                  <EyeOff className="h-3.5 w-3.5" />
                  {t_i18n("Dismiss finding")}
                </Button>
              </Tip>
            )}
          </div>
          {finding.reopened && <ReopenedNote reopening={finding.reopened} />}
          <p className="content-compact leading-relaxed text-default-secondary">
            <ConfigText text={finding.description} />
          </p>
          {finding.recommendation && (
            <div className="flex gap-2 rounded-sm border border-accent/20 bg-accent/5 p-3" data-testid="security-audit-recommendation">
              <Lightbulb className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" />
              <div className="min-w-0 space-y-0.5 content-compact">
                <p className="font-medium text-default-primary">{t_i18n("Recommendation")}</p>
                <p className="leading-relaxed text-default-secondary">
                  <ConfigText text={finding.recommendation} />
                </p>
                {linkedOption && <OptionLink optionKey={linkedOption} />}
                {finding.id && API_KEY_FINDING_VIEWS[finding.id] && <ApiKeyListLink to={API_KEY_FINDING_VIEWS[finding.id]} />}
              </div>
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

/** The admin API key list, filtered on the keys a finding is about. */
const API_KEY_FINDING_VIEWS: Record<string, string> = {
  "authentication.api_keys_without_expiry": "/settings/security/api-keys?status=active&issue=no_expiry",
  "authentication.api_keys_beyond_max_lifetime": "/settings/security/api-keys?status=active&issue=beyond_max_lifetime",
  "authentication.api_keys_full_scope": "/settings/security/api-keys?status=active&issue=full_scope",
};

function ApiKeyListLink({ to }: { to: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Link
      to={to}
      className="mt-1.5 flex w-fit items-center gap-1.5 rounded-sm font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      data-testid="security-audit-api-keys-link"
    >
      <KeyRound className="h-3.5 w-3.5" />
      {t_i18n("Review these API keys")}
    </Link>
  );
}

/** The link from a finding or a control to the Security options field that sets it. */
function OptionLink({ optionKey }: { optionKey: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Link
      to={optionLocation(optionKey)}
      className="mt-1.5 flex w-fit items-center gap-1.5 rounded-sm font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      data-testid="security-audit-option-link"
      data-option-key={optionKey}
    >
      <SlidersHorizontal className="h-3.5 w-3.5" />
      {t_i18n("Configure {option}", { option: t_i18n(optionLabel(optionKey)) })}
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Dismissed findings: accepted by an administrator
// ---------------------------------------------------------------------------

function formatInstant(value: string | null | undefined, intl: ReturnType<typeof useFormatter>["intl"]): string | null {
  const at = value ? Date.parse(value) : Number.NaN;
  return Number.isNaN(at) ? null : intl.formatDate(at, { dateStyle: "medium", timeStyle: "short" });
}

/** Who dismissed a finding, when, and why. */
function DismissalMeta({ dismissal, severity }: { dismissal: SecurityAuditFindingDismissal; severity: string }) {
  const { t_i18n, intl } = useFormatter();
  const who = dismissal.by ?? (dismissal.by_user_id ? t_i18n("an administrator") : t_i18n("a deleted account"));
  const date = formatInstant(dismissal.at, intl);
  return (
    <div className="space-y-1 content-compact text-default-secondary" data-testid="security-audit-dismissal">
      <p>
        {date ? t_i18n("Dismissed by {who} on {date}", { who, date }) : t_i18n("Dismissed by {who}", { who })}
        {dismissal.severity !== severity && (
          <>
            {" - "}
            {t_i18n("dismissed as {severity}", { severity: t_i18n(severityLabel(dismissal.severity)) })}
          </>
        )}
      </p>
      {dismissal.reason && (
        <blockquote
          className="whitespace-pre-line break-words border-l-2 border-elevation-subtle pl-2 italic"
          data-testid="security-audit-dismissal-reason"
        >
          {dismissal.reason}
        </blockquote>
      )}
    </div>
  );
}

/** A dismissed finding reported again at a higher severity: it counts again and was notified. */
function ReopenedNote({ reopening }: { reopening: SecurityAuditFindingReopening }) {
  const { t_i18n, intl } = useFormatter();
  const tone = toneOf(reopening.severity);
  const date = formatInstant(reopening.at, intl);
  const previous = reopening.dismissal;
  return (
    <div
      role="note"
      className={cn("flex gap-2 rounded-sm border p-3 content-compact", tone.border, tone.soft)}
      data-testid="security-audit-reopened"
    >
      <BellRing className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", tone.text)} />
      <div className="min-w-0 space-y-1">
        <p className="font-medium text-default-primary">
          {date ? t_i18n("Reopened on {date}", { date }) : t_i18n("Reopened")}
        </p>
        <p className="leading-relaxed text-default-secondary">
          {previous
            ? t_i18n(
                "It was dismissed as {dismissed} and is now reported as {severity}, so it counts again and the administrators were notified.",
                {
                  dismissed: t_i18n(severityLabel(previous.severity)),
                  severity: t_i18n(severityLabel(reopening.severity)),
                },
              )
            : t_i18n(
                "It is now reported at a higher severity than when it was dismissed, so it counts again and the administrators were notified.",
              )}
        </p>
        {previous && <DismissalMeta dismissal={previous} severity={previous.severity} />}
      </div>
    </div>
  );
}

function DismissedFindingsPanel({
  findings,
  focused,
  restoringId,
  onRestore,
}: {
  findings: SecurityAuditFinding[];
  /** Ids a Security options link asked for: the list opens on its own when it holds one. */
  focused: ReadonlySet<string>;
  /** The finding whose restore is in flight. */
  restoringId: string | null;
  onRestore: (finding: SecurityAuditFinding) => void;
}) {
  const { t_i18n } = useFormatter();
  const listId = useId();
  const holdsFocused = findings.some((finding) => !!finding.id && focused.has(finding.id));
  // Collapsed until asked for; a Security options link to one of these findings opens it.
  const [toggled, setToggled] = useState<boolean | null>(null);
  const open = toggled ?? holdsFocused;
  const keys = findingKeys(findings);
  return (
    <Card data-testid="security-audit-dismissed">
      <CardHeader className="space-y-1.5 pb-4">
        <h3 className="text-base font-semibold leading-none tracking-tight">
          <button
            type="button"
            aria-expanded={open}
            aria-controls={listId}
            onClick={() => setToggled(!open)}
            data-testid="security-audit-dismissed-toggle"
            className="flex w-full items-center gap-2 rounded-sm text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            <EyeOff className="h-4 w-4 shrink-0 text-default-secondary" />
            {t_i18n("Dismissed findings")}
            <span className="rounded-sm bg-elevation-hover px-1.5 content-compact font-medium tabular-nums">{findings.length}</span>
            <ChevronDown
              className={cn("ml-auto h-4 w-4 shrink-0 text-default-secondary transition-transform", open && "rotate-180")}
              aria-hidden="true"
            />
          </button>
        </h3>
        <CardDescription>
          {t_i18n(
            "Accepted by an administrator: they are out of the counts and the score, and never notified. A finding reported again at a higher severity reopens.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent id={listId} hidden={!open}>
        <ul className="space-y-3">
          {findings.map((finding, index) => (
            <DismissedFindingItem
              key={keys[index]}
              finding={finding}
              focused={!!finding.id && focused.has(finding.id)}
              restoring={restoringId !== null && finding.id === restoringId}
              onRestore={() => onRestore(finding)}
            />
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function DismissedFindingItem({
  finding,
  focused,
  restoring,
  onRestore,
}: {
  finding: SecurityAuditFinding;
  focused: boolean;
  restoring: boolean;
  onRestore: () => void;
}) {
  const { t_i18n } = useFormatter();
  const tone = toneOf(finding.severity);
  const Icon = SEVERITY_ICONS[finding.severity] ?? CircleDot;
  const CategoryIcon = CATEGORY_ICONS[finding.category] ?? Layers;
  return (
    <li
      data-testid="security-audit-dismissed-finding"
      data-finding-id={finding.id ?? undefined}
      data-severity={finding.severity}
      data-focused={focused ? "true" : undefined}
      className={cn("rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-4", focused && "ring-2 ring-accent/60")}
    >
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-sm bg-elevation-hover">
          <Icon className="h-4 w-4 text-default-secondary" />
        </span>
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <h5 className="content-base font-medium text-default-primary">{finding.title}</h5>
            <Badge variant="outline" className={cn("content-caption font-medium", tone.text, tone.border)}>
              {t_i18n(severityLabel(finding.severity))}
            </Badge>
            <Badge variant="outline" className="gap-1 content-caption font-medium text-default-secondary">
              <CategoryIcon className="h-3 w-3" />
              {t_i18n(categoryLabel(finding.category))}
            </Badge>
            <Tip content={t_i18n("Count it again: it returns to the findings and the score.")}>
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto h-7 gap-1.5 px-2 content-compact text-default-secondary hover:text-default-primary"
                onClick={onRestore}
                disabled={restoring}
                aria-busy={restoring}
                data-testid="security-audit-restore"
              >
                <RotateCcw className={cn("h-3.5 w-3.5", restoring && "animate-spin")} />
                {t_i18n("Restore")}
              </Button>
            </Tip>
          </div>
          {finding.dismissed && <DismissalMeta dismissal={finding.dismissed} severity={finding.severity} />}
          <p className="content-compact leading-relaxed text-default-secondary">
            <ConfigText text={finding.description} />
          </p>
        </div>
      </div>
    </li>
  );
}

function DismissFindingDialog({
  finding,
  pending,
  onCancel,
  onConfirm,
}: {
  finding: SecurityAuditFinding;
  pending: boolean;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const [reason, setReason] = useState("");
  const reasonId = useId();
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !pending) onCancel();
      }}
    >
      <DialogContent className="max-w-lg" data-testid="security-audit-dismiss-dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <EyeOff className="h-4 w-4 text-accent" />
            {t_i18n("Dismiss this finding?")}
          </DialogTitle>
          <DialogDescription>{finding.title}</DialogDescription>
        </DialogHeader>
        <ul className="list-disc space-y-1.5 pl-4 content-compact leading-relaxed text-default-secondary">
          <li>{t_i18n("It leaves the counts and the score, and the administrators are never notified about it.")}</li>
          <li>
            {t_i18n(
              "If a later run reports it at a higher severity, it reopens: it counts again and the administrators are notified once.",
            )}
          </li>
          <li>{t_i18n("You can restore it at any time. Dismissing and restoring are recorded in the audit log.")}</li>
        </ul>
        <div className="space-y-1.5">
          <Label htmlFor={reasonId}>{t_i18n("Reason (optional)")}</Label>
          <textarea
            id={reasonId}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={t_i18n("Why is this finding acceptable on this platform?")}
            maxLength={DISMISSAL_REASON_MAX_CHARS}
            data-testid="security-audit-dismiss-reason"
            className="min-h-24 w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base outline-none ring-offset-focus placeholder:text-default-secondary focus-visible:ring-2 focus-visible:ring-focus"
          />
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={pending}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            onClick={() => onConfirm(reason)}
            disabled={pending}
            aria-busy={pending}
            className="bg-accent text-negative-primary hover:bg-accent/90"
            data-testid="security-audit-dismiss-confirm"
          >
            {pending ? t_i18n("Dismissing...") : t_i18n("Dismiss finding")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Controls: every security switch and whether it is enforced
// ---------------------------------------------------------------------------

function ViewSwitch({
  view,
  onView,
  findings,
  controls,
}: {
  view: AuditView;
  onView: (view: AuditView) => void;
  findings: number;
  controls: number;
}) {
  const { t_i18n } = useFormatter();
  const options = [
    { key: "findings", label: t_i18n("Findings"), count: findings, icon: ListFilter },
    { key: "controls", label: t_i18n("Controls"), count: controls, icon: ToggleRight },
  ] as const;
  return (
    <div
      role="group"
      aria-label={t_i18n("Report view")}
      className="inline-flex items-center gap-1 rounded-sm bg-elevation-hover p-1"
      data-testid="security-audit-views"
    >
      {options.map(({ key, label, count, icon: Icon }) => (
        <button
          key={key}
          type="button"
          aria-pressed={view === key}
          onClick={() => onView(key)}
          data-testid="security-audit-view"
          data-view={key}
          className={cn(
            "flex items-center gap-1.5 rounded-sm px-3 py-1.5 content-compact font-medium transition-all",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
            view === key
              ? "bg-accent/10 text-accent shadow-xs"
              : "text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
          )}
        >
          <Icon className="h-3.5 w-3.5" />
          {label}
          <span className="rounded-sm bg-elevation-hover px-1.5 tabular-nums">{count}</span>
        </button>
      ))}
    </div>
  );
}

function ControlsPanel({
  controls,
  findings,
  options,
}: {
  controls: SecurityControlStatus[];
  findings: SecurityAuditFinding[];
  /** The security options, once read: a control links to the option it is, unless its variable locks it. */
  options: SecurityOption[] | undefined;
}) {
  const { t_i18n, locale } = useFormatter();
  const linked = useMemo(() => findingsByControl(findings), [findings]);
  const ordered = useMemo(() => sortControls(controls, linked), [controls, linked]);
  const totals = useMemo(() => tallyControls(controls), [controls]);
  const categories = useMemo(() => controlCategories(controls), [controls]);
  const [filters, setFilters] = useState<ControlFilters>(NO_CONTROL_FILTERS);
  // The search also matches the labels the screen shows, which are translated.
  const labelsOf = useCallback(
    (control: SecurityControlStatus) =>
      `${t_i18n(controlStateLabel(control.state))} ${t_i18n(categoryLabel(control.category))}`,
    [t_i18n],
  );
  const visible = useMemo(() => filterControls(ordered, filters, labelsOf), [ordered, filters, labelsOf]);

  if (controls.length === 0) {
    return (
      <div data-testid="security-audit-controls-empty">
        <EmptyState icon={ShieldCheck} title="No security control reported" description="The audit ran and reported no security control." />
      </div>
    );
  }

  const reported = [...categories];
  // A filter kept across a re-run may name a category the new report no longer has.
  if (filters.category !== null && !reported.includes(filters.category)) reported.push(filters.category);
  const categoryOptions = sortByLabel(reported, (category) => t_i18n(categoryLabel(category)), locale);
  const clear = () => setFilters(NO_CONTROL_FILTERS);

  return (
    <Card data-testid="security-audit-controls">
      <CardHeader className="gap-4 space-y-0 pb-4">
        <div className="space-y-1.5">
          <CardTitle className="text-base">{t_i18n("Security controls")}</CardTitle>
          <CardDescription>
            {t_i18n(
              "Every security switch of the platform and its state: enforced, monitoring (it records what it would block, without blocking it) or off.",
            )}
          </CardDescription>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <SearchInput
            placeholder="Search controls..."
            value={filters.query}
            onChange={(query) => setFilters((current) => ({ ...current, query }))}
            className="w-full"
            inputClassName="h-9"
          />
          <Select
            value={filters.category ?? ALL_CATEGORIES}
            onValueChange={(value) =>
              setFilters((current) => ({ ...current, category: value === ALL_CATEGORIES ? null : value }))
            }
          >
            <SelectTrigger
              className="h-9 w-full sm:w-56"
              aria-label={t_i18n("Filter by category")}
              data-testid="security-audit-control-category-filter"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_CATEGORIES}>{t_i18n("All categories")}</SelectItem>
              {categoryOptions.map((category) => (
                <SelectItem key={category} value={category}>
                  {t_i18n(categoryLabel(category))}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {hasActiveControlFilters(filters) && (
            <Button
              variant="ghost"
              size="sm"
              className="gap-1.5 self-start sm:ml-auto sm:self-auto"
              onClick={clear}
              data-testid="security-audit-controls-clear-filters"
            >
              <X className="h-3.5 w-3.5" />
              {t_i18n("Clear filters")}
            </Button>
          )}
        </div>
        <div role="group" aria-label={t_i18n("Filter by state")} className="flex flex-wrap items-center gap-1.5">
          {totals.states.map(({ state, count }) => {
            const pressed = filters.states.includes(state);
            const tone = TONE_CLASSES[controlStateTone(state)];
            const Icon = CONTROL_STATE_ICONS[state] ?? CircleDot;
            return (
              <button
                key={state}
                type="button"
                aria-pressed={pressed}
                disabled={count === 0 && !pressed}
                onClick={() =>
                  setFilters((current) => ({ ...current, states: toggleArrayItem([...current.states], state) }))
                }
                data-testid="security-audit-control-state-filter"
                data-state={state}
                className={cn(
                  "inline-flex h-9 items-center gap-1.5 rounded-sm border px-3 content-compact font-medium transition-colors",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:opacity-40",
                  pressed
                    ? cn(tone.border, tone.soft, tone.text)
                    : "border-elevation-subtle-soft text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
                )}
              >
                <Icon className={cn("h-3.5 w-3.5", tone.text)} />
                {t_i18n(controlStateLabel(state))}
                <span className="rounded-sm bg-elevation-hover px-1.5 tabular-nums">{count}</span>
              </button>
            );
          })}
        </div>
        <p className="content-compact text-default-secondary" aria-live="polite" data-testid="security-audit-control-result-count">
          {t_i18n("{total, plural, one {Showing {shown} of # control} other {Showing {shown} of # controls}}", {
            shown: visible.length,
            total: controls.length,
          })}
        </p>
      </CardHeader>
      <CardContent>
        {visible.length === 0 ? (
          <EmptyState
            variant="inset"
            icon={SearchX}
            title="No matching controls"
            description="No control matches the current filters."
            actionLabel="Clear filters"
            actionIcon={X}
            onAction={clear}
          />
        ) : (
          <ul className="space-y-3">
            {visible.map((control) => (
              <ControlItem
                key={control.id}
                control={control}
                linked={linked.get(control.id) ?? []}
                option={controlOption(control, options)}
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function ControlItem({
  control,
  linked,
  option,
}: {
  control: SecurityControlStatus;
  linked: readonly SecurityAuditFinding[];
  /** The Security options field this control is, or `null`. */
  option: SecurityOption | null;
}) {
  const { t_i18n } = useFormatter();
  const locked = option ? lockMessage(option, t_i18n) : null;
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  const tone = TONE_CLASSES[controlStateTone(control.state)];
  const StateIcon = CONTROL_STATE_ICONS[control.state] ?? CircleDot;
  const CategoryIcon = CATEGORY_ICONS[control.category] ?? Layers;
  // Linked findings come most urgent first; a dismissed one never stands for its control.
  const worst = worstLinkedFinding(linked);
  const flagged = control.state === "off" && worst !== null && (worst.severity === "critical" || worst.severity === "warning");
  return (
    <li
      data-testid="security-audit-control"
      data-control-id={control.id}
      data-state={control.state}
      data-category={control.category}
      className={cn("rounded-sm border layer-1 bg-elevation-default", flagged && worst ? toneOf(worst.severity).border : "border-elevation-subtle-soft")}
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls={detailsId}
        onClick={() => setOpen((current) => !current)}
        data-testid="security-audit-control-toggle"
        className="flex w-full items-start gap-3 rounded-sm p-4 text-left transition-colors hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        <span className={cn("mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-sm", tone.soft)}>
          <StateIcon className={cn("h-4 w-4", tone.text)} />
        </span>
        <span className="min-w-0 flex-1 space-y-2">
          <span className="flex flex-wrap items-center gap-2">
            <span className="content-base font-medium text-default-primary">{control.title}</span>
            <Badge
              variant="outline"
              className={cn("content-caption font-medium", tone.text, tone.border)}
              data-testid="security-audit-control-state"
            >
              {t_i18n(controlStateLabel(control.state))}
            </Badge>
            <Badge variant="outline" className="gap-1 content-caption font-medium text-default-secondary">
              <CategoryIcon className="h-3 w-3" />
              {t_i18n(categoryLabel(control.category))}
            </Badge>
            {control.opt_in && (
              <Badge
                variant="outline"
                className="content-caption font-medium text-default-secondary"
                data-testid="security-audit-control-opt-in"
              >
                {t_i18n("Opt-in")}
              </Badge>
            )}
            {locked &&
              (option?.locked_reason === "platform_mode" ? (
                <ReadOnlyBadge tooltip={locked} />
              ) : (
                <EnvLockBadge tooltip={locked} />
              ))}
          </span>
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1 content-compact text-default-secondary">
            {control.stored_in === "config" ? (
              <ConfigCode data-testid="security-audit-control-setting">{control.setting}</ConfigCode>
            ) : (
              <span data-testid="security-audit-control-setting">{control.where}</span>
            )}
            <span className="inline-flex items-center gap-1" data-testid="security-audit-control-values">
              <ConfigCode>{control.value}</ConfigCode>
              <ArrowRight className="h-3 w-3 shrink-0" aria-hidden="true" />
              <ConfigCode>{control.hardened}</ConfigCode>
            </span>
          </span>
          {worst && (
            <span
              className="flex min-w-0 items-center gap-1.5 content-compact text-default-secondary"
              data-testid="security-audit-control-finding"
              data-severity={worst.severity}
            >
              <span className={cn("h-2 w-2 shrink-0 rounded-full", toneOf(worst.severity).dot)} aria-hidden="true" />
              <span className="sr-only">{t_i18n(severityLabel(worst.severity))}</span>
              <span className="truncate">{worst.title}</span>
            </span>
          )}
        </span>
        <ChevronDown
          className={cn("mt-1 h-4 w-4 shrink-0 text-default-secondary transition-transform", open && "rotate-180")}
          aria-hidden="true"
        />
      </button>
      <ControlDetails id={detailsId} hidden={!open} control={control} linked={linked} option={option} />
    </li>
  );
}

type Translate = ReturnType<typeof useFormatter>["t_i18n"];

/** How an administrator enforces a control: the variable to set, or the Settings page a stored one lives in. */
function howToChange(control: SecurityControlStatus, t_i18n: Translate): string {
  if (control.stored_in !== "config") return t_i18n("Change it in {where}.", { where: control.where });
  const setting = control.setting;
  const phrase = hardenedPhrase(control.hardened);
  switch (phrase.kind) {
    case "empty":
      return t_i18n("Leave {setting} unset or empty and restart the platform.", { setting });
    case "atLeast":
      return t_i18n("Set {setting} to {value} or more and restart the platform.", { setting, value: phrase.value });
    case "atMost":
      return t_i18n("Set {setting} to {value} or less and restart the platform.", { setting, value: phrase.value });
    case "above":
      return t_i18n("Set {setting} above {value} and restart the platform.", { setting, value: phrase.value });
    case "below":
      return t_i18n("Set {setting} below {value} and restart the platform.", { setting, value: phrase.value });
    case "value":
      return /\s/.test(phrase.value)
        ? t_i18n("Set {setting} to {value} and restart the platform.", { setting, value: phrase.value })
        : t_i18n("Set {assignment} and restart the platform.", { assignment: `${setting}=${phrase.value}` });
  }
}

function ControlDetails({
  id,
  hidden,
  control,
  linked,
  option,
}: {
  id: string;
  hidden: boolean;
  control: SecurityControlStatus;
  linked: readonly SecurityAuditFinding[];
  option: SecurityOption | null;
}) {
  const { t_i18n } = useFormatter();
  const keys = findingKeys(linked);
  const linkedOption = controlOptionKey(control, option ? [option] : undefined);
  return (
    <div
      id={id}
      hidden={hidden}
      className="space-y-3 border-t border-elevation-subtle-soft px-4 pb-4 pt-3 content-compact leading-relaxed text-default-secondary"
      data-testid="security-audit-control-details"
    >
      <p>
        <span className="font-medium text-default-primary">{t_i18n("Enforcing it:")}</span> <ConfigText text={control.impact} />
      </p>
      {control.evidence && (
        <p data-testid="security-audit-control-evidence">
          <span className="font-medium text-default-primary">{t_i18n("Before enforcing it, read:")}</span>{" "}
          <ConfigText text={control.evidence} />
        </p>
      )}
      <div data-testid="security-audit-control-how" data-pinned={control.pinned || undefined}>
        <p>
          <span className="font-medium text-default-primary">{t_i18n("How to change it:")}</span>{" "}
          {control.pinned ? (
            t_i18n("It cannot be changed: XTM One fixes its value.")
          ) : (
            <ConfigText text={howToChange(control, t_i18n)} />
          )}
        </p>
        {linkedOption && <OptionLink optionKey={linkedOption} />}
      </div>
      {option && (
        <p
          data-testid="security-audit-control-source"
          data-source={option.source}
          data-locked={option.locked || undefined}
          className={cn(option.locked && "text-feedback-warning-primary light:text-feedback-warning-primary")}
        >
          <span className="font-medium text-default-primary">{t_i18n("Where its value comes from:")}</span>{" "}
          <ConfigText text={optionSourceMessage(option, t_i18n)} />
        </p>
      )}
      <p data-testid="security-audit-control-default">
        <span className="font-medium text-default-primary">{t_i18n("Platform default:")}</span>{" "}
        <ConfigCode>{control.default}</ConfigCode>{" "}
        {control.opt_in
          ? t_i18n("It leaves the control off: enforcing it is an opt-in.")
          : t_i18n("It enforces the control.")}
      </p>
      {linked.length > 0 && (
        <div className="space-y-2">
          <p className="font-medium text-default-primary">{t_i18n("Findings about this control")}</p>
          <ul className="space-y-2">
            {linked.map((finding, index) => {
              const findingTone = toneOf(finding.severity);
              const Icon = SEVERITY_ICONS[finding.severity] ?? CircleDot;
              const dismissed = isDismissed(finding);
              return (
                <li
                  key={keys[index]}
                  className={cn(
                    "rounded-sm border p-3",
                    finding.severity === "pass" || dismissed ? "border-elevation-subtle-soft" : findingTone.border,
                  )}
                  data-testid="security-audit-control-linked-finding"
                  data-finding-id={finding.id ?? undefined}
                  data-severity={finding.severity}
                  data-dismissed={dismissed || undefined}
                >
                  <p className="flex items-center gap-1.5 font-medium text-default-primary">
                    <Icon className={cn("h-3.5 w-3.5 shrink-0", dismissed ? "text-default-secondary" : findingTone.text)} />
                    {finding.title}
                    <span className="sr-only">{t_i18n(severityLabel(finding.severity))}</span>
                    {dismissed && (
                      <Badge variant="outline" className="ml-1 gap-1 content-caption font-medium text-default-secondary">
                        <EyeOff className="h-3 w-3" />
                        {t_i18n("Dismissed")}
                      </Badge>
                    )}
                  </p>
                  <p className="mt-1">
                    <ConfigText text={finding.description} />
                  </p>
                  {finding.recommendation && (
                    <p className="mt-1">
                      <span className="font-medium text-default-primary">{t_i18n("Recommendation")}</span>
                      {" - "}
                      <ConfigText text={finding.recommendation} />
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
