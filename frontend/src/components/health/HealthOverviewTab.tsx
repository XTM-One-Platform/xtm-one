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
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { DateTimeCell } from "@/components/ui/date-time-cell";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { StatCard } from "@/components/ui/stat-card";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  useDismissHealthFindings,
  useHealthFindings,
  useHealthHistory,
  useHealthOverview,
  useRestoreHealthFindings,
} from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { EVALUATION_CHART_COLORS } from "@/lib/constants";
import { HEALTH_MODULE_LABELS, categoryLabel, checkTitle, evidenceLabel, moduleCardSubtitle } from "@/lib/health-modules";
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

function evidenceValue(value: unknown, t: Translate): string {
  if (typeof value === "boolean") return value ? t("Yes") : t("No");
  if (Array.isArray(value)) return value.map((item) => evidenceValue(item, t)).join(", ");
  return typeof value === "object" && value !== null ? JSON.stringify(value) : String(value);
}

/** The evidence as translated label and value pairs. */
function evidenceLines(finding: HealthFinding, t: Translate): [string, string][] {
  const values = (finding.evidence?.values ?? {}) as Record<string, unknown>;
  const lines = Object.entries(values).map(([key, value]) => [evidenceLabel(key, t), evidenceValue(value, t)] as [string, string]);
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
          <SettingsField
            id="health-dismiss-reason"
            label={t_i18n("Reason")}
            required
            helper={t_i18n("Recorded in the audit trail.")}
            error={missing ? t_i18n("Give a reason.") : undefined}
          >
            <Textarea
              id="health-dismiss-reason"
              autoFocus
              rows={3}
              maxLength={500}
              disabled={busy}
              value={reason}
              aria-required
              aria-describedby={settingsFieldHintId("health-dismiss-reason")}
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
          <p className="content-base text-default-primary">{finding.recommendation}</p>
        </div>
      ) : null}
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
        {t_i18n("Check")}: <span className="font-mono-plex">{finding.check_id}</span>
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

export function HealthOverviewTab() {
  const { t_i18n } = useFormatter();
  const overview = useHealthOverview();
  const findings = useHealthFindings({ status: ["open", "dismissed", "resolved"], limit: 200 });
  const restore = useRestoreHealthFindings();
  const [view, setView] = useState<FindingsView>("open");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string[]>([]);
  const [dismissing, setDismissing] = useState<HealthFinding[]>([]);
  const cards = useMemo(() => overview.data?.modules ?? [], [overview.data]);
  const modules = useMemo(() => cards.map((card) => card.module), [cards]);
  const all = useMemo(() => findings.data?.items ?? [], [findings.data]);
  const counts = useMemo(
    () => ({
      open: all.filter((finding) => finding.status === "open").length,
      dismissed: all.filter((finding) => finding.status === "dismissed").length,
      resolved: all.filter((finding) => finding.status === "resolved").length,
    }),
    [all],
  );
  const rows = useMemo(() => all.filter((finding) => finding.status === view), [all, view]);
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
        cell: (finding) => <CellText text={findingTitle(finding, t_i18n)} />,
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
        options: [...new Set(all.map((finding) => finding.category))]
          .map((value) => ({ value, label: categoryLabel(value, t_i18n) }))
          .sort((a, b) => a.label.localeCompare(b.label)),
      },
    ],
    [t_i18n, modules, all],
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
        { key: "open", label: t_i18n("Open findings"), count: counts.open, countPending: findings.isLoading },
        { key: "dismissed", label: t_i18n("Dismissed findings"), count: counts.dismissed, countPending: findings.isLoading },
        { key: "resolved", label: t_i18n("Resolved findings"), count: counts.resolved, countPending: findings.isLoading },
      ]}
    />
  );

  const bulk =
    view === "open" ? (
      <Button variant="outline" size="sm" disabled={!target.length} onClick={() => setDismissing(target)} data-testid="health-dismiss-all">
        {picked.length
          ? t_i18n("{count, plural, one {Dismiss # finding} other {Dismiss # findings}}", { count: picked.length })
          : t_i18n("Dismiss all")}
      </Button>
    ) : view === "dismissed" ? (
      <Button
        variant="outline"
        size="sm"
        disabled={!target.length || restore.isPending}
        onClick={() => restoreFindings(target)}
        data-testid="health-restore-all"
      >
        {picked.length
          ? t_i18n("{count, plural, one {Restore # finding} other {Restore # findings}}", { count: picked.length })
          : t_i18n("Restore all")}
      </Button>
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
          />
        ))}
      </div>
      {overview.data?.scope === "all" ? <ScoreHistory modules={modules} /> : null}
      {!findings.isLoading && !findings.isError && !all.length ? (
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
          toolbar={bulk}
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
