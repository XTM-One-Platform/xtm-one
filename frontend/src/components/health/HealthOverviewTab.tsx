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
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { StatCard } from "@/components/ui/stat-card";
import {
  useDismissHealthFinding,
  useHealthFindings,
  useHealthHistory,
  useHealthOverview,
} from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { EVALUATION_CHART_COLORS } from "@/lib/constants";
import { HEALTH_MODULE_LABELS, moduleCardSubtitle } from "@/lib/health-modules";
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

const STATUS_LABELS: Record<string, string> = {
  open: "Still open",
  resolved: "Resolved",
  dismissed: "Set aside",
};

const HISTORY_WINDOWS = [7, 30, 90] as const;
type HistoryWindow = (typeof HISTORY_WINDOWS)[number];

/** Column widths in px under the fixed layout: Finding takes what is left. */
const FINDING_COLUMN_WIDTHS = { severity: 112, module: 120, status: 120, seen: 152 } as const;

const AXIS_TICK = { fill: "var(--text-default-secondary)", fontSize: 11 };
const AXIS_STROKE = "var(--border-elevation-subtle)";

/** The finding's title: its check, then the agent it is about when there is one. */
function findingTitle(finding: HealthFinding): string {
  return finding.subject_name ? `${finding.check_id} · ${finding.subject_name}` : finding.check_id;
}

function evidenceLines(finding: HealthFinding): [string, string][] {
  const values = (finding.evidence?.values ?? {}) as Record<string, unknown>;
  const lines = Object.entries(values).map(([key, value]) => [key, typeof value === "object" ? JSON.stringify(value) : String(value)] as [string, string]);
  const threshold = finding.evidence?.threshold;
  if (threshold !== undefined && threshold !== null) lines.push(["threshold", String(threshold)]);
  return lines;
}

function DismissDialog({ finding, onClose }: { finding: HealthFinding | null; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  const dismiss = useDismissHealthFinding();
  const [reason, setReason] = useState("");
  const [days, setDays] = useState<number | null>(30);
  const busy = dismiss.isPending;
  const canSubmit = reason.trim().length > 0 && !busy && finding !== null;
  const close = () => {
    if (busy) return;
    setReason("");
    onClose();
  };
  const submit = () => {
    if (!canSubmit || finding === null) return;
    dismiss.mutate({ id: finding.id, reason: reason.trim(), days }, { onSuccess: close });
  };
  return (
    <Dialog open={finding !== null} onOpenChange={(next) => (!next ? close() : undefined)}>
      <DialogContent className="max-w-md" data-testid="health-dismiss-dialog">
        <DialogHeader>
          <DialogTitle className="text-base">{t_i18n("Dismiss this finding?")}</DialogTitle>
          <DialogDescription className="content-base leading-relaxed">
            {t_i18n("It leaves the open list and the score until it lapses or the check passes. The reason is recorded in the audit trail.")}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3 pt-1">
          <div className="space-y-2">
            <label htmlFor="health-dismiss-reason" className="content-compact text-default-secondary">
              {t_i18n("Reason")}
            </label>
            <textarea
              id="health-dismiss-reason"
              autoFocus
              rows={3}
              maxLength={500}
              disabled={busy}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder={t_i18n("Known and accepted, fixed in the next release…")}
              className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base text-default-primary placeholder:text-default-secondary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus:border-accent disabled:opacity-50"
            />
          </div>
          <fieldset className="space-y-2">
            <legend className="content-compact text-default-secondary">{t_i18n("For how long")}</legend>
            <div className="flex flex-wrap gap-2">
              {([7, 30, 90, null] as const).map((value) => (
                <Button
                  key={String(value)}
                  type="button"
                  size="sm"
                  variant={days === value ? "secondary" : "outline"}
                  aria-pressed={days === value}
                  disabled={busy}
                  onClick={() => setDays(value)}
                >
                  {value === null ? t_i18n("Until it resolves") : t_i18n("{count, plural, one {# day} other {# days}}", { count: value })}
                </Button>
              ))}
            </div>
          </fieldset>
          {dismiss.isError ? (
            <p role="alert" className="content-compact text-feedback-error-primary">
              {t_i18n("The finding could not be dismissed.")}
            </p>
          ) : null}
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" size="sm" onClick={close} disabled={busy}>
            {t_i18n("Cancel")}
          </Button>
          <Button size="sm" disabled={!canSubmit} onClick={submit} data-testid="health-dismiss-confirm">
            {busy ? t_i18n("Setting aside…") : t_i18n("Dismiss finding")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function FindingDetail({ finding, onDismiss }: { finding: HealthFinding; onDismiss: (finding: HealthFinding) => void }) {
  const { t_i18n } = useFormatter();
  const lines = evidenceLines(finding);
  return (
    <div className="space-y-3 px-1 py-2" data-testid="health-finding-detail">
      {finding.recommendation ? (
        <div className="space-y-1">
          <h4 className="content-compact font-medium text-default-secondary">{t_i18n("Recommendation")}</h4>
          <p className="content-base text-default-primary">{finding.recommendation}</p>
        </div>
      ) : null}
      {lines.length ? (
        <div className="space-y-1">
          <h4 className="content-compact font-medium text-default-secondary">{t_i18n("Evidence")}</h4>
          <dl className="grid grid-cols-[minmax(0,12rem)_minmax(0,1fr)] gap-x-4 gap-y-1 content-compact">
            {lines.map(([key, value]) => (
              <div key={key} className="contents">
                <dt className="truncate font-mono-plex text-default-secondary" title={key}>
                  {key}
                </dt>
                <dd className="min-w-0 break-words font-mono-plex text-default-primary">{value}</dd>
              </div>
            ))}
          </dl>
        </div>
      ) : null}
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

export function HealthOverviewTab() {
  const { t_i18n } = useFormatter();
  const overview = useHealthOverview();
  const findings = useHealthFindings({ status: ["open", "dismissed", "resolved"], limit: 200 });
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [dismissing, setDismissing] = useState<HealthFinding | null>(null);
  const cards = useMemo(() => overview.data?.modules ?? [], [overview.data]);
  const modules = useMemo(() => cards.map((card) => card.module), [cards]);
  const rows = useMemo(() => findings.data?.items ?? [], [findings.data]);

  const columns = useMemo<DataTableColumn<HealthFinding>[]>(
    () => [
      {
        key: "severity",
        header: "Severity",
        width: FINDING_COLUMN_WIDTHS.severity,
        cell: (finding) => (
          <Badge variant={SEVERITY_BADGE[finding.severity]} className="max-w-full whitespace-nowrap">
            <span className="truncate">{t_i18n(SEVERITY_LABELS[finding.severity])}</span>
          </Badge>
        ),
        sortValue: (finding) => ["critical", "warning", "info"].indexOf(finding.severity),
        csv: (finding) => t_i18n(SEVERITY_LABELS[finding.severity]),
      },
      {
        key: "finding",
        header: "Finding",
        cell: (finding) => (
          <span className="block min-w-0 truncate" title={findingTitle(finding)}>
            {findingTitle(finding)}
          </span>
        ),
        sortValue: (finding) => findingTitle(finding),
        csv: (finding) => findingTitle(finding),
      },
      {
        key: "module",
        header: "Module",
        width: FINDING_COLUMN_WIDTHS.module,
        cell: (finding) => (
          <span className="block truncate whitespace-nowrap" title={t_i18n(HEALTH_MODULE_LABELS[finding.module])}>
            {t_i18n(HEALTH_MODULE_LABELS[finding.module])}
          </span>
        ),
        sortValue: (finding) => t_i18n(HEALTH_MODULE_LABELS[finding.module]),
        csv: (finding) => t_i18n(HEALTH_MODULE_LABELS[finding.module]),
      },
      {
        key: "status",
        header: "Status",
        hideable: true,
        width: FINDING_COLUMN_WIDTHS.status,
        cell: (finding) => (
          <span className="block truncate whitespace-nowrap" title={t_i18n(STATUS_LABELS[finding.status] ?? finding.status)}>
            {t_i18n(STATUS_LABELS[finding.status] ?? finding.status)}
          </span>
        ),
        csv: (finding) => t_i18n(STATUS_LABELS[finding.status] ?? finding.status),
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
        options: [...new Set(rows.map((finding) => finding.category))].sort().map((value) => ({ value, label: value })),
      },
      {
        key: "status",
        label: "Status",
        values: (finding) => [finding.status],
        options: Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label: t_i18n(label) })),
      },
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
          />
        ))}
      </div>
      {overview.data?.scope === "all" ? <ScoreHistory modules={modules} /> : null}
      {!findings.isLoading && !findings.isError && !rows.length ? (
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
          expandedRow={(finding) => (open.has(finding.id) ? <FindingDetail finding={finding} onDismiss={setDismissing} /> : null)}
          layout="fixed"
          searchFields={(finding) => [findingTitle(finding), finding.recommendation, finding.category]}
          searchPlaceholder={t_i18n("Search...")}
          filters={filters}
          exportName="health-findings"
          urlKey="hf_"
          storageKey="health-findings"
          isLoading={findings.isLoading}
          isError={findings.isError}
          errorLabel={t_i18n("Platform Health findings could not be loaded.")}
          onRetry={() => void findings.refetch()}
          emptyLabel={t_i18n("No finding")}
        />
      )}
      <DismissDialog finding={dismissing} onClose={() => setDismissing(null)} />
    </div>
  );
}
