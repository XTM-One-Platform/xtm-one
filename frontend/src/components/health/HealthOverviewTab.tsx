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
 * Platform Health overview (#101 S8, dev-docs/platform-health.md section 8): one card per
 * module present (agents and fleets in every mode; XTM One, OpenCTI, OpenAEV and OpenCRQ in
 * the XTM One modes, which the API decides), the score history over 7, 30 or 90 days, and the
 * findings list with each finding's evidence, recommendation and dismissal. Every figure is
 * read from the engine's store: no model call, nothing metered.
 */
import { useMemo, useState, type ComponentProps } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Icon, Textarea } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { DataTable, TOOLBAR_CONTROL_HEIGHT, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { StatCard } from "@/components/ui/stat-card";
import { InfoTip } from "@/components/ui/info-tip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { ModuleAnalysisSummary, analysisRowFor } from "@/components/health/ModuleAnalysisSummary";
import { ANALYST_MODULES, AnalystRecommendationBlock, ModuleAnalystBlock } from "@/components/health/ModuleAnalystParts";
import { ModuleCheckReportLine } from "@/components/health/ModuleCheckReport";
import {
  useDismissHealthFindings,
  useHealthAnalysis,
  useHealthFindings,
  useHealthHistory,
  useHealthOverview,
  useRestoreHealthFindings,
} from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { EVALUATION_CHART_COLORS } from "@/lib/constants";
import { formatDateTime } from "@/lib/utils";
import {
  HEALTH_MODULE_LABELS,
  categoryLabel,
  checkRecommendation,
  checkTitle,
  evidenceLabel,
  moduleCardSubtitle,
  periodLabel,
} from "@/lib/health-modules";
import type { HealthFinding, HealthModule, HealthSeverity } from "@/types";

/** The health glyph, rendered by the library <Icon name> (no direct lucide-react import). */
const HealthGlyph = (({ className }: { className?: string }) => (
  <Icon name="activity" size={16} className={className} aria-hidden />
)) as unknown as ComponentProps<typeof StatCard>["icon"];

const SEVERITY_LABELS: Record<HealthSeverity, string> = {
  critical: "Critical",
  warning: "Warning",
  info: "Info",
};

const SEVERITY_BADGE: Record<HealthSeverity, "destructive" | "warning" | "secondary"> = {
  critical: "destructive",
  warning: "warning",
  info: "secondary",
};

/** Where a finding comes from: a deterministic check or the Module Analyst (#101 S14c). @i18n-keys */
const SOURCE_LABELS = {
  // "Checks" is the checkbox field's key (fr "Coches"): the source has its own words (UX round 1 B1 on #953).
  checks: "Health checks",
  agentic: "Agentic",
} as const;

const sourceOf = (finding: HealthFinding): keyof typeof SOURCE_LABELS => (finding.agentic ? "agentic" : "checks");

const HISTORY_WINDOWS = [7, 30, 90] as const;
type HistoryWindow = (typeof HISTORY_WINDOWS)[number];

/**
 * Column widths in px under the fixed layout: Finding takes what is left. Severity holds the
 * longest badge of the four catalogs ("Avertissement", UX round 1 B3 on #634); every other
 * cut label is named whole by the design-system tooltip.
 */
const FINDING_COLUMN_WIDTHS = { severity: 152, module: 128, category: 152, seen: 176 } as const;

type DismissDays = "7" | "30" | "90" | "resolved";
const DISMISS_DAYS: DismissDays[] = ["7", "30", "90", "resolved"];

const AXIS_TICK = { fill: "var(--text-default-secondary)", fontSize: 11 };
const AXIS_STROKE = "var(--border-elevation-subtle)";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/** The finding's title: its check's translated title, then the agent it is about when there is one. */
function findingTitle(finding: HealthFinding, t: Translate): string {
  const title = checkTitle(finding.check_id, t);
  return finding.subject_name ? `${title} · ${finding.subject_name}` : title;
}

/** A time of the evidence (a key ending in _at) is a formatted date, a missing one "Never" (UX round 1 B2 on #2724). */
function evidenceTime(value: unknown, t: Translate): string {
  if (value === null || value === undefined || value === "") return t("Never");
  return typeof value === "string" && !Number.isNaN(Date.parse(value)) ? formatDateTime(value) : String(value);
}

function evidenceValue(value: unknown, t: Translate): string {
  if (typeof value === "boolean") return value ? t("Yes") : t("No");
  if (Array.isArray(value)) return value.map((item) => evidenceValue(item, t)).join(", ");
  return typeof value === "object" && value !== null ? JSON.stringify(value) : String(value);
}

/** The evidence as translated label and value pairs. */
function evidenceLines(finding: HealthFinding, t: Translate): [string, string][] {
  const values = (finding.evidence?.values ?? {}) as Record<string, unknown>;
  // A quota period is a word to translate (UX round 2 B2 on #634: never "month" raw).
  const lines = Object.entries(values).map(
    ([key, value]) =>
      [
        evidenceLabel(key, t),
        key === "period" && typeof value === "string" ? periodLabel(value, t) : key.endsWith("_at") ? evidenceTime(value, t) : evidenceValue(value, t),
      ] as [string, string],
  );
  const threshold = finding.evidence?.threshold;
  if (threshold !== undefined && threshold !== null) lines.push([evidenceLabel("threshold", t), evidenceValue(threshold, t)]);
  return lines;
}

/** A cell's text: one line, ended with an ellipsis when cut and then named whole by the design-system tooltip. */
function CellText({ text }: { text: string }) {
  return (
    <span className="block min-w-0">
      <TruncatedText text={text} />
    </span>
  );
}

/**
 * Dismiss one finding (the row's action) or several (Dismiss all), with one reason and one
 * duration (UX round 1 B4, B5 on #634): the reason is a design-system Textarea under a body-size
 * label with its required mark and help line, the duration a radio group whose choice shows.
 */
function DismissDialog({ findings, onClose }: { findings: HealthFinding[]; onClose: (done: boolean) => void }) {
  const { t_i18n } = useFormatter();
  const dismiss = useDismissHealthFindings();
  const [reason, setReason] = useState("");
  const [days, setDays] = useState<DismissDays>("30");
  const [tried, setTried] = useState(false);
  const busy = dismiss.isPending;
  const open = findings.length > 0;
  const missing = tried && !reason.trim();
  const close = (done: boolean) => {
    if (busy) return;
    setReason("");
    setTried(false);
    dismiss.reset();
    onClose(done);
  };
  const submit = () => {
    setTried(true);
    if (!reason.trim() || busy || !open) return;
    dismiss.mutate(
      { ids: findings.map((finding) => finding.id), reason: reason.trim(), days: days === "resolved" ? null : Number(days) },
      { onSuccess: () => close(true) },
    );
  };
  const title =
    findings.length > 1
      ? t_i18n("{count, plural, one {Dismiss # finding?} other {Dismiss # findings?}}", { count: findings.length })
      : t_i18n("Dismiss this finding?");
  return (
    <Dialog open={open} onOpenChange={(next) => (!next ? close(false) : undefined)}>
      <DialogContent className="max-w-lg" data-testid="health-dismiss-dialog">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription className="content-base leading-relaxed">
            {t_i18n("It leaves the open list and the score until it lapses or the check passes. You can restore it from Dismissed findings.")}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-4">
          {/* The design-system Textarea draws the missing-reason error itself: error border, aria-invalid
              and the message in its helper row (UX round 2 B3 on #634); the field's helper steps aside. */}
          <SettingsField
            id="health-dismiss-reason"
            label={t_i18n("Reason")}
            required
            helper={missing ? undefined : t_i18n("Recorded in the audit trail.")}
          >
            <Textarea
              id="health-dismiss-reason"
              error={missing ? t_i18n("Give a reason.") : undefined}
              autoFocus
              rows={3}
              maxLength={500}
              disabled={busy}
              value={reason}
              aria-required
              aria-labelledby={settingsFieldLabelId("health-dismiss-reason")}
              aria-describedby={missing ? undefined : settingsFieldHintId("health-dismiss-reason")}
              onChange={(e) => setReason(e.target.value)}
              placeholder={t_i18n("Known and accepted, fixed in the next release…")}
            />
          </SettingsField>
          <SettingsField id="health-dismiss-days" label={t_i18n("For how long")}>
            <SegmentedFilter<DismissDays>
              selection="radio"
              size="md"
              ariaLabel={t_i18n("For how long")}
              testId="health-dismiss-days"
              value={days}
              onChange={setDays}
              options={DISMISS_DAYS.map((key) => ({
                key,
                label: key === "resolved" ? t_i18n("Until it resolves") : t_i18n("{count, plural, one {# day} other {# days}}", { count: Number(key) }),
                disabled: busy,
              }))}
            />
          </SettingsField>
          {dismiss.isError ? (
            <p role="alert" className="content-compact text-feedback-error-primary">
              {t_i18n("The finding could not be dismissed.")}
            </p>
          ) : null}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={() => close(false)} disabled={busy}>
            {t_i18n("Cancel")}
          </Button>
          <Button onClick={submit} disabled={busy} data-testid="health-dismiss-confirm">
            {busy ? t_i18n("Setting aside…") : t_i18n("Dismiss finding")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function FindingDetail({
  finding,
  onDismiss,
  onRestore,
  restoring,
}: {
  finding: HealthFinding;
  onDismiss: (finding: HealthFinding) => void;
  onRestore: (finding: HealthFinding) => void;
  restoring: boolean;
}) {
  const { t_i18n } = useFormatter();
  const lines = evidenceLines(finding, t_i18n);
  return (
    <div className="space-y-3 px-1 py-2" data-testid="health-finding-detail">
      {finding.recommendation ? (
        <div className="space-y-1">
          <h4 className="content-compact font-medium text-default-secondary">{t_i18n("Recommendation")}</h4>
          <p className="content-base text-default-primary">{checkRecommendation(finding.check_id, finding.recommendation, t_i18n)}</p>
        </div>
      ) : null}
      {finding.analyst_recommendation ? <AnalystRecommendationBlock recommendation={finding.analyst_recommendation} /> : null}
      {lines.length ? (
        <div className="space-y-1">
          <h4 className="content-compact font-medium text-default-secondary">{t_i18n("Evidence")}</h4>
          <dl className="grid grid-cols-[minmax(0,14rem)_minmax(0,1fr)] gap-x-4 gap-y-1 content-compact">
            {lines.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="min-w-0 text-default-secondary">
                  <TruncatedText text={label} />
                </dt>
                <dd className="min-w-0 break-words text-default-primary">{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
      <p className="content-compact text-default-secondary">
        {t_i18n("Check: (health check id)")} <span className="font-mono-plex">{finding.check_id}</span>
      </p>
      {finding.dismissal ? (
        <p className="content-compact text-default-secondary">
          {t_i18n("Set aside by {name}: {reason}", { name: finding.dismissal.by_name || "-", reason: finding.dismissal.reason })}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {finding.doc_link ? (
          <Button variant="outline" size="sm" asChild>
            <a href={finding.doc_link} target="_blank" rel="noreferrer noopener">
              {t_i18n("Documentation")}
            </a>
          </Button>
        ) : null}
        {finding.can_dismiss ? (
          <Button variant="outline" size="sm" onClick={() => onDismiss(finding)}>
            {t_i18n("Dismiss finding")}
          </Button>
        ) : null}
        {finding.status === "dismissed" ? (
          <Button variant="outline" size="sm" disabled={restoring} onClick={() => onRestore(finding)} data-testid="health-restore">
            {t_i18n("Restore")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function ScoreHistory({ modules }: { modules: HealthModule[] }) {
  const { t_i18n, locale } = useFormatter();
  const [days, setDays] = useState<HistoryWindow>(30);
  const { data: points = [], isLoading } = useHealthHistory(days);
  const rows = useMemo(() => {
    const byDay = new Map<string, Record<string, number | string>>();
    for (const point of points) {
      const row = byDay.get(point.day) ?? { day: point.day };
      row[point.module] = point.score;
      byDay.set(point.day, row);
    }
    return [...byDay.values()];
  }, [points]);
  const shown = modules.filter((module) => points.some((point) => point.module === module));
  return (
    <section className="space-y-3 rounded-md border border-elevation-subtle bg-elevation-default p-4" aria-labelledby="health-history-title">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="health-history-title" className="content-base font-medium text-default-primary">
          {t_i18n("Score history")}
        </h3>
        <div className="flex gap-1" role="group" aria-label={t_i18n("History window")}>
          {HISTORY_WINDOWS.map((value) => (
            <Button key={value} size="sm" variant={days === value ? "secondary" : "ghost"} aria-pressed={days === value} onClick={() => setDays(value)}>
              {t_i18n("{count, plural, one {# day} other {# days}}", { count: value })}
            </Button>
          ))}
        </div>
      </div>
      {!isLoading && !rows.length ? (
        <p className="content-compact text-default-secondary">{t_i18n("No score was written in this window yet.")}</p>
      ) : (
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={AXIS_STROKE} opacity={0.4} />
              <XAxis
                dataKey="day"
                tick={AXIS_TICK}
                stroke={AXIS_STROKE}
                tickFormatter={(day: string) => new Date(`${day}T00:00:00Z`).toLocaleDateString(locale, { month: "short", day: "numeric" })}
                minTickGap={16}
              />
              <YAxis domain={[0, 100]} tick={AXIS_TICK} stroke={AXIS_STROKE} width={32} />
              <Tooltip {...CHART_TOOLTIP_PROPS} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {shown.map((module, index) => (
                <Line
                  key={module}
                  type="monotone"
                  dataKey={module}
                  name={t_i18n(HEALTH_MODULE_LABELS[module])}
                  stroke={EVALUATION_CHART_COLORS[index % EVALUATION_CHART_COLORS.length]}
                  strokeWidth={2}
                  dot={rows.length <= 16}
                  connectNulls
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}

type FindingsView = "open" | "dismissed" | "resolved";

/** Rows read per status: the endpoint's page cap (``MAX_PAGE`` in platform_health_findings.py). */
const FINDINGS_PAGE = 200;

export function HealthOverviewTab() {
  const { t_i18n } = useFormatter();
  const overview = useHealthOverview();
  const isAdmin = overview.data?.scope === "all";
  // Each module card shows the outcome of its analysis (#961); stored rows, no model call.
  const analysis = useHealthAnalysis({ enabled: isAdmin });
  // One list per status, each with its own page and its own server total (#634 review P2): a shared page of
  // the three statuses let resolved and dismissed rows push open ones off it once they accumulate.
  const openFindings = useHealthFindings({ status: ["open"], limit: FINDINGS_PAGE });
  const dismissedFindings = useHealthFindings({ status: ["dismissed"], limit: FINDINGS_PAGE });
  const resolvedFindings = useHealthFindings({ status: ["resolved"], limit: FINDINGS_PAGE });
  const restore = useRestoreHealthFindings();
  const [view, setView] = useState<FindingsView>("open");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string[]>([]);
  const [dismissing, setDismissing] = useState<HealthFinding[]>([]);
  const cards = useMemo(() => overview.data?.modules ?? [], [overview.data]);
  const modules = useMemo(() => cards.map((card) => card.module), [cards]);
  // One AI block per module card (#965, #953), administrators only: the newest of the module's analysis
  // summary (#961) and the module checker's last check round (section 13.2, named after Run checks now,
  // UX round 1 B1 on #1269). An analysed module with no round shows its summary; a module with no newer
  // summary shows the Last check round line. Both bodies share one header, and an analyst card's menu always holds Analyse now and
  // Run checks now (UX round 6 B1, B2). No row to show, no footer: a card never carries an empty divider.
  const moduleFooter = (card: (typeof cards)[number]) => {
    if (!isAdmin) return undefined;
    const hasOpenFindings = card.critical + card.warning + card.info > 0;
    const row = analysisRowFor(card.module, hasOpenFindings, analysis.data);
    const investigated = ANALYST_MODULES.includes(card.module);
    const investigation = card.last_investigation;
    const investigatedAt = investigation ? Date.parse(investigation.closed_at ?? investigation.opened_at) : Number.NaN;
    const analysedAt = row?.analysed_at ? Date.parse(row.analysed_at) : Number.NaN;
    const summaryIsNewer = row !== null && (Number.isNaN(investigatedAt) || analysedAt > investigatedAt);
    if (investigated) {
      // The agentic checks line (#101 S15e, section 13.6) under the card's one AI block: stored reports, no model.
      // Under the Last check round block it carries the counts only: no second "No check round yet".
      return (
        <div className="space-y-3">
          <ModuleAnalystBlock module={card.module} investigation={investigation} showSummary={summaryIsNewer} hasOpenFindings={hasOpenFindings} overview={analysis.data} />
          <ModuleCheckReportLine module={card.module} hideEmpty={!summaryIsNewer} />
        </div>
      );
    }
    return row ? <ModuleAnalysisSummary module={card.module} hasOpenFindings={hasOpenFindings} overview={analysis.data} /> : undefined;
  };
  const lists = { open: openFindings, dismissed: dismissedFindings, resolved: resolvedFindings };
  const findings = lists[view];
  // Tab counts come from each list's server total, never from the rows of a page.
  const counts = {
    open: openFindings.data?.total ?? 0,
    dismissed: dismissedFindings.data?.total ?? 0,
    resolved: resolvedFindings.data?.total ?? 0,
  };
  const rows = useMemo(() => findings.data?.items ?? [], [findings.data]);
  const hidden = Math.max(0, (findings.data?.total ?? 0) - rows.length);
  const listsLoading = openFindings.isLoading || dismissedFindings.isLoading || resolvedFindings.isLoading;
  const nothingFound =
    !listsLoading &&
    !openFindings.isError &&
    !dismissedFindings.isError &&
    !resolvedFindings.isError &&
    !counts.open &&
    !counts.dismissed &&
    !counts.resolved;
  // Bulk acts on what this view holds and the reader may act on: open findings they may dismiss, dismissed ones.
  const actionable = useMemo(
    () => rows.filter((finding) => (view === "open" ? finding.can_dismiss : view === "dismissed")),
    [rows, view],
  );
  const picked = useMemo(() => {
    const keys = new Set(selected);
    return actionable.filter((finding) => keys.has(finding.id));
  }, [actionable, selected]);
  // Selected rows first; none selected, the whole view (UX round 1 B6 on #634: bulk first).
  const target = picked.length ? picked : actionable;

  const switchView = (next: FindingsView) => {
    setView(next);
    setSelected([]);
  };

  const columns = useMemo<DataTableColumn<HealthFinding>[]>(
    () => [
      {
        key: "severity",
        header: "Severity",
        width: FINDING_COLUMN_WIDTHS.severity,
        cell: (finding) => (
          <Badge variant={SEVERITY_BADGE[finding.severity]} className="whitespace-nowrap">
            {t_i18n(SEVERITY_LABELS[finding.severity])}
          </Badge>
        ),
        sortValue: (finding) => ["critical", "warning", "info"].indexOf(finding.severity),
        csv: (finding) => t_i18n(SEVERITY_LABELS[finding.severity]),
      },
      {
        key: "finding",
        header: "Finding",
        cell: (finding) =>
          finding.agentic ? (
            <span className="flex min-w-0 items-center gap-2">
              <Badge variant="outline" className="shrink-0 whitespace-nowrap" data-testid="health-finding-agentic">
                {t_i18n(SOURCE_LABELS.agentic)}
              </Badge>
              <CellText text={findingTitle(finding, t_i18n)} />
            </span>
          ) : (
            <CellText text={findingTitle(finding, t_i18n)} />
          ),
        sortValue: (finding) => findingTitle(finding, t_i18n),
        csv: (finding) => findingTitle(finding, t_i18n),
      },
      {
        key: "module",
        header: "Module",
        width: FINDING_COLUMN_WIDTHS.module,
        cell: (finding) => <CellText text={t_i18n(HEALTH_MODULE_LABELS[finding.module])} />,
        sortValue: (finding) => t_i18n(HEALTH_MODULE_LABELS[finding.module]),
        csv: (finding) => t_i18n(HEALTH_MODULE_LABELS[finding.module]),
      },
      {
        key: "category",
        header: "Category",
        hideable: true,
        width: FINDING_COLUMN_WIDTHS.category,
        cell: (finding) => <CellText text={categoryLabel(finding.category, t_i18n)} />,
        sortValue: (finding) => categoryLabel(finding.category, t_i18n),
        csv: (finding) => categoryLabel(finding.category, t_i18n),
      },
      {
        key: "seen",
        header: "Last seen",
        hideable: true,
        width: FINDING_COLUMN_WIDTHS.seen,
        cell: (finding) => <DateTimeCell value={finding.last_seen} />,
        sortValue: (finding) => finding.last_seen,
        csv: (finding) => finding.last_seen,
      },
    ],
    [t_i18n],
  );

  const filters = useMemo<DataTableFilter<HealthFinding>[]>(
    () => [
      {
        key: "severity",
        label: "Severity",
        values: (finding) => [finding.severity],
        options: Object.entries(SEVERITY_LABELS).map(([value, label]) => ({ value, label: t_i18n(label) })),
      },
      {
        key: "module",
        label: "Module",
        values: (finding) => [finding.module],
        options: modules.map((module) => ({ value: module, label: t_i18n(HEALTH_MODULE_LABELS[module]) })),
      },
      {
        key: "category",
        label: "Category",
        values: (finding) => [finding.category],
        options: [...new Set(rows.map((finding) => finding.category))]
          .map((value) => ({ value, label: categoryLabel(value, t_i18n) }))
          .sort((a, b) => a.label.localeCompare(b.label)),
      },
      // Checks or Module Analyst (section 12.7): offered once the view holds an agentic finding.
      ...(rows.some((finding) => finding.agentic)
        ? [
            {
              key: "source",
              label: "Source",
              values: (finding: HealthFinding) => [sourceOf(finding)],
              options: Object.entries(SOURCE_LABELS).map(([value, label]) => ({ value, label: t_i18n(label) })),
            },
          ]
        : []),
    ],
    [t_i18n, modules, rows],
  );

  const toggle = (finding: HealthFinding) =>
    setOpen((previous) => {
      const next = new Set(previous);
      if (next.has(finding.id)) next.delete(finding.id);
      else next.add(finding.id);
      return next;
    });

  const restoreFindings = (items: HealthFinding[]) => {
    if (!items.length) return;
    restore.mutate({ ids: items.map((finding) => finding.id) }, { onSuccess: () => setSelected([]) });
  };

  const viewSwitch = (
    <SegmentedFilter<FindingsView>
      value={view}
      onChange={switchView}
      ariaLabel={t_i18n("Findings shown")}
      testId="health-findings-view"
      options={[
        { key: "open", label: t_i18n("Open findings"), count: counts.open, countPending: openFindings.isLoading },
        { key: "dismissed", label: t_i18n("Dismissed findings"), count: counts.dismissed, countPending: dismissedFindings.isLoading },
        { key: "resolved", label: t_i18n("Resolved findings"), count: counts.resolved, countPending: resolvedFindings.isLoading },
      ]}
    />
  );

  const bulk =
    view === "open" ? (
      <Button
        variant="outline"
        size="sm"
        className={TOOLBAR_CONTROL_HEIGHT}
        disabled={!target.length}
        onClick={() => setDismissing(target)}
        data-testid="health-dismiss-all"
      >
        {picked.length
          ? t_i18n("{count, plural, one {Dismiss # finding} other {Dismiss # findings}}", { count: picked.length })
          : t_i18n("Dismiss all (health findings)")}
      </Button>
    ) : view === "dismissed" ? (
      <Button
        variant="outline"
        size="sm"
        className={TOOLBAR_CONTROL_HEIGHT}
        disabled={!target.length || restore.isPending}
        onClick={() => restoreFindings(target)}
        data-testid="health-restore-all"
      >
        {picked.length
          ? t_i18n("{count, plural, one {Restore # finding} other {Restore # findings}}", { count: picked.length })
          : t_i18n("Restore all")}
      </Button>
    ) : null;
  // A cut list says so in the toolbar, next to its "200 of 205" total, never below the list
  // (UX round 4 B2 on #634, standards s.3 "nothing below the list").
  const truncatedLabel = hidden
    ? t_i18n("{count, plural, one {# more finding is not shown: the list keeps the most severe and most recent ones.} other {# more findings are not shown: the list keeps the most severe and most recent ones.}}", { count: hidden })
    : "";
  const truncated = hidden ? (
    // The shared InfoTip (hover colour, help cursor, design-system focus ring), in the 32 px toolbar slot
    // (UX round 5 B1 on #634).
    <span className="inline-flex shrink-0" data-testid="health-findings-truncated">
      <InfoTip label={truncatedLabel} content={truncatedLabel} className={`${TOOLBAR_CONTROL_HEIGHT} w-8 items-center justify-center`} />
    </span>
  ) : null;
  // The server total of the view goes to the table as totalAll: the chip reads "200 of 205" when the list
  // is cut (UX round 4 B1 on #634).
  const toolbar =
    truncated || bulk ? (
      <>
        {truncated}
        {bulk}
      </>
    ) : null;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3" data-testid="health-module-cards">
        {cards.map((card) => (
          <StatCard
            key={card.module}
            icon={HealthGlyph}
            label={t_i18n(HEALTH_MODULE_LABELS[card.module])}
            value={String(card.score)}
            subtitle={moduleCardSubtitle(card, t_i18n)}
            iconClassName={card.critical ? "text-feedback-error-primary" : card.warning ? "text-feedback-warning-primary" : undefined}
            footer={moduleFooter(card)}
          />
        ))}
      </div>
      {overview.data?.scope === "all" ? <ScoreHistory modules={modules} /> : null}
      {nothingFound ? (
        <EmptyState
          icon={HealthGlyph}
          title={t_i18n("No finding")}
          description={t_i18n("Checks run on a schedule without any model call; what they find shows here.")}
        />
      ) : (
        <DataTable
          rows={rows}
          columns={columns}
          rowKey={(finding) => finding.id}
          onRowClick={toggle}
          expandedRow={(finding) =>
            open.has(finding.id) ? (
              <FindingDetail
                finding={finding}
                onDismiss={(item) => setDismissing([item])}
                onRestore={(item) => restoreFindings([item])}
                restoring={restore.isPending}
              />
            ) : null
          }
          layout="fixed"
          toolbarLead={viewSwitch}
          toolbar={toolbar}
          totalAll={findings.data?.total}
          selection={
            view === "resolved"
              ? undefined
              : {
                  selected,
                  onChange: setSelected,
                  disabledReason: (key) =>
                    actionable.some((finding) => finding.id === key) ? undefined : t_i18n("You may not dismiss this finding."),
                }
          }
          searchFields={(finding) => [findingTitle(finding, t_i18n), finding.check_id, finding.recommendation, categoryLabel(finding.category, t_i18n)]}
          searchPlaceholder={t_i18n("Search...")}
          filters={filters}
          exportName="health-findings"
          urlKey="hf_"
          storageKey="health-findings"
          isLoading={findings.isLoading}
          isError={findings.isError}
          errorLabel={t_i18n("Platform Health findings could not be loaded.")}
          onRetry={() => void findings.refetch()}
          emptyLabel={view === "open" ? t_i18n("No open finding") : view === "dismissed" ? t_i18n("No dismissed finding") : t_i18n("No resolved finding")}
        />
      )}
      {restore.isError ? (
        <p role="alert" className="content-compact text-feedback-error-primary">
          {t_i18n("The findings could not be restored.")}
        </p>
      ) : null}
      <DismissDialog
        findings={dismissing}
        onClose={(done) => {
          setDismissing([]);
          if (done) setSelected([]);
        }}
      />
    </div>
  );
}
