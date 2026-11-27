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
 * The parts of an ROI report drawn the same way wherever a report is read
 * (design dev-docs/roi-outcomes.md 6.3): the Analytics Outcomes tab (#9706)
 * and the fleet ROI view (#9785). The three KPI cards, the "estimated" note,
 * the rows table, the assumptions card and the loading skeleton. Reading a
 * report never starts a run nor calls a model.
 */
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Gauge, Info } from "lucide-react";
import { useIntl } from "react-intl";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { InfoTip } from "@/components/ui/info-tip";
import { CompactFigure } from "@/components/ui/compact-figure";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { DataTable, type DataTableColumn, type DataTableFilter } from "@/components/ui/data-table";
import { Skeleton } from "@/components/ui/skeleton";
import { RoiFigure } from "@/components/roi/RoiFigure";
import type { RoiAssumptionFact, RoiComputationStep, RoiReportResponse, RoiWindow } from "@/hooks/api/useRoiReportApi";
import { useFormatter } from "@/i18n";
import { exactCount, exactCurrency, formatCount, formatCurrency } from "@/lib/number-format";
import { roiRecordCounts, SOURCE_NAMES } from "@/lib/roi-figure";
import { KIND_LABELS, outcomeRowPath, type OutcomeRemainder, type OutcomeRow } from "@/lib/roi-outcomes";
import { roiReviewCounts } from "@/lib/roi-records";

type RoiFigures = RoiReportResponse["organization"];

/** How a computation line writes its figures: compact for the text, exact for its hover (#10770). */
interface LineFormat {
  count: (value: number) => string;
  money: (value: number, currency: string) => string;
}

/** The labels the API puts on a minute or a rate figure (`services/roi.py` LABEL_*). @i18n-keys */
const FIGURE_LABELS = {
  assumption: "assumption",
  "published estimate": "published estimate",
  measured: "measured",
  "organization override": "organization override",
  "organization unit": "organization unit",
  "low: published estimate; likely and high: assumption": "published estimate and assumption",
} as const;

/** Settings > Organization > Units of work, where the minutes and the rates are set. */
const UNITS_SETTINGS_PATH = "/settings/organization/units";

/**
 * The report's table cells (#10476): every figure compact in the reader's locale and the report
 * currency (no cents from 100, cents below, four decimals below one cent), its exact value on
 * hover; a rate as a whole percent ("-" without a run). Sort and CSV read the raw value.
 */
function useRoiFormat(currency: string) {
  const f = useNumberFormat(currency);
  return useMemo(
    () => ({
      count: (value: number) => <CompactFigure exact={f.exactCount(value)}>{f.count(value)}</CompactFigure>,
      money: (amount: number) => <CompactFigure exact={f.exactMoney(amount)}>{f.money(amount)}</CompactFigure>,
      percent: (rate: number | null) => <CompactFigure exact={f.exactPercent(rate)}>{f.percent(rate)}</CompactFigure>,
    }),
    [f],
  );
}

/**
 * The table columns: name, then the kind when the table mixes fleets and
 * agents, runs, units, hours saved, cost, net value, reviewed share. Runs and
 * units step aside in a narrow card (1024 px, long fr / es headers) instead of
 * a sideways scroll.
 */
function useOutcomeColumns(currency: string, options: { withKind: boolean }): DataTableColumn<OutcomeRow>[] {
  const { t_i18n } = useFormatter();
  const { count, money, percent } = useRoiFormat(currency);
  const { withKind } = options;
  return useMemo<DataTableColumn<OutcomeRow>[]>(
    () => [
      {
        key: "name",
        header: t_i18n("Name"),
        // Plain text: the row opens where the agent or the fleet lives (rowHref, #12070).
        cell: (row) => (
          <span title={row.name} className="block max-w-[12rem] truncate font-medium">
            {row.name}
          </span>
        ),
        sortValue: (row) => row.name,
        csv: (row) => row.name,
      },
      ...(withKind
        ? [
            {
              key: "kind",
              header: t_i18n("Type"),
              hideable: true,
              cell: (row: OutcomeRow) => t_i18n(KIND_LABELS[row.kind]),
              sortValue: (row: OutcomeRow) => row.kind,
              csv: (row: OutcomeRow) => row.kind,
            },
          ]
        : []),
      { key: "runs", header: t_i18n("Runs"), align: "right", hideBelow: "4xl", hideable: true, cell: (row) => count(row.runs), sortValue: (row) => row.runs, csv: (row) => row.runs },
      { key: "units", header: t_i18n("Units"), align: "right", hideBelow: "4xl", hideable: true, cell: (row) => count(row.units), sortValue: (row) => row.units, csv: (row) => row.units },
      { key: "hours", header: t_i18n("Hours saved"), align: "right", hideable: true, cell: (row) => count(row.hoursSaved), sortValue: (row) => row.hoursSaved, csv: (row) => row.hoursSaved },
      { key: "cost", header: t_i18n("Cost"), align: "right", hideable: true, cell: (row) => (row.cost === null ? t_i18n("Hidden") : money(row.cost)), sortValue: (row) => row.cost ?? -1, csv: (row) => row.cost ?? "" },
      { key: "net", header: t_i18n("Net value"), align: "right", hideable: true, cell: (row) => (row.netValue === null ? t_i18n("Hidden") : money(row.netValue)), sortValue: (row) => row.netValue ?? -1, csv: (row) => row.netValue ?? "" },
      { key: "reviewed", header: t_i18n("Reviewed"), align: "right", hideable: true, cell: (row) => percent(row.reviewRate), sortValue: (row) => row.reviewRate ?? -1, csv: (row) => row.reviewRate ?? "" },
    ],
    [t_i18n, withKind, count, money, percent],
  );
}

interface RoiBreakdownProps {
  rows: OutcomeRow[];
  currency: string;
  /** The kind column, for a table that mixes fleets and agents. */
  withKind: boolean;
  urlKey: string;
  storageKey: string;
  /** Already translated. */
  searchPlaceholder: string;
  /** The caller's filters, or `noFilters` with the reason it needs none. */
  filters?: DataTableFilter<OutcomeRow>[];
  noFilters?: string;
  /** Summed rows drawn last (no agent, other agents), so the table sums to the hero (#12077 L6b3 UX r2 B6). */
  remainders?: OutcomeRemainder[];
}

/** The first cell of a remainder row: plain text, its reason in a tooltip, one line like a name cell. */
function OutcomeRemainderLabel({ remainder }: { remainder: OutcomeRemainder }) {
  const { t_i18n } = useFormatter();
  const label = t_i18n(remainder.label);
  return (
    <span className="flex min-w-0 max-w-[12rem] items-center gap-1">
      <span className="min-w-0 truncate" title={label} data-truncate="true">
        {label}
      </span>
      <span className="shrink-0">
        <InfoTip label={label} content={t_i18n(remainder.hint)} />
      </span>
    </span>
  );
}

/** One row per fleet or agent of a report, by net value; searchable and sortable on every figure. */
export function RoiBreakdown({ rows, currency, withKind, urlKey, storageKey, searchPlaceholder, filters, noFilters, remainders }: RoiBreakdownProps) {
  const { t_i18n } = useFormatter();
  const columns = useOutcomeColumns(currency, { withKind });
  return (
    <DataTable
      urlKey={urlKey}
      storageKey={storageKey}
      rows={rows}
      columns={columns}
      rowKey={(row) => `${row.kind}:${row.id}`}
      rowHref={outcomeRowPath}
      defaultSort={{ key: "net", direction: "desc" }}
      searchFields={(row) => [row.name]}
      searchPlaceholder={searchPlaceholder}
      filters={filters}
      noFilters={noFilters}
      remainderRows={remainders?.map((r) => ({ key: r.key, row: r.row, label: <OutcomeRemainderLabel remainder={r} />, testId: `roi-remainder-${r.key}` }))}
      emptyLabel={t_i18n("Nothing was counted in this period.")}
    />
  );
}

/**
 * Hours saved, net value (with the cost under it) and review rate of one report
 * scope. `onReview` makes the review-rate card open the scope's ROI records
 * list (`roiRecordsPath`, #10570): runs and work items alike, under the same
 * window, so the total the caption says is the number of rows the list holds.
 * Review is optional (Sam, 10 Nov 2026): the caption never reads as a backlog.
 */
export function RoiKpiCards({
  figures,
  currency,
  window,
  costMasked,
  onReview,
}: {
  figures: RoiFigures;
  currency: string;
  /** The report's window, named in the "How this is computed" popover. */
  window: RoiWindow;
  costMasked?: boolean;
  onReview?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const f = useNumberFormat(currency);
  const costSubtitle =
    figures.llm_cost === null
      ? undefined
      : figures.runs > 0 && figures.llm_cost === 0
      ? t_i18n("Cost {cost}: no priced model or no run cost recorded", { cost: f.money(0) })
      : t_i18n("Cost {cost}", { cost: f.money(figures.llm_cost) });
  // A work item's record is not a run: said apart, so the counts match the runs list the review card opens (#9810).
  // The popover reads the same split (roiRecordCounts), so the two never differ (UX #9961 r2 B1).
  const { runs: runRecords, workItems } = roiRecordCounts(figures);
  // Each count is compact, its exact line on hover (#10770): the plural reads the number, {shown} draws it.
  // The ledger hero counts chat deliverables beside the runs: the caption names them, as the popover does (#12077 L6b3 UX r2 B4).
  // Runs and work items keep the record split there too, so caption and popover never differ (UX r3 B8).
  const chats = figures.deliverables ?? 0;
  const countLine = (shown: (value: number) => string) =>
    chats > 0 && workItems > 0
      ? t_i18n(
          "{runs, plural, one {{runsShown} run} other {{runsShown} runs}}, {items, plural, one {{itemsShown} work item} other {{itemsShown} work items}}, {chats, plural, one {{chatsShown} chat deliverable} other {{chatsShown} chat deliverables}}",
          { runs: runRecords, runsShown: shown(runRecords), items: workItems, itemsShown: shown(workItems), chats, chatsShown: shown(chats) },
        )
      : chats > 0
      ? t_i18n("{runs, plural, one {{runsShown} run} other {{runsShown} runs}}, {chats, plural, one {{chatsShown} chat deliverable} other {{chatsShown} chat deliverables}}", {
          runs: runRecords,
          runsShown: shown(runRecords),
          chats,
          chatsShown: shown(chats),
        })
      : workItems > 0
      ? t_i18n("{runs, plural, one {{runsShown} run} other {{runsShown} runs}}, {items, plural, one {{itemsShown} work item} other {{itemsShown} work items}}", {
          runs: runRecords,
          runsShown: shown(runRecords),
          items: workItems,
          itemsShown: shown(workItems),
        })
      : t_i18n("{count, plural, one {{shown} run} other {{shown} runs}}", { count: figures.runs, shown: shown(figures.runs) });
  const countSubtitle = (
    <CompactFigure className="whitespace-normal" exact={countLine(f.exactCount)}>
      {countLine(f.count)}
    </CompactFigure>
  );
  // The rate's denominator is said: every record, runs and work items (#10570); none reads "-" (RoiFigure).
  const review = roiReviewCounts(figures);
  const reviewLine = (shown: (value: number) => string) =>
    t_i18n("Reviewed {reviewed} of {total, plural, one {{count} record} other {{count} records}} (optional)", {
      reviewed: shown(review.reviewed),
      total: review.total,
      count: shown(review.total),
    });
  const reviewSubtitle =
    review.total > 0 ? (
      <CompactFigure className="whitespace-normal" exact={reviewLine(f.exactCount)}>
        {reviewLine(f.count)}
      </CompactFigure>
    ) : undefined;
  // Every figure is drawn by the one RoiFigure (#9836 R4b, design 10.5): the range is the caption
  // when the report has one, else the count or the cost line; a masked cost reads "Hidden".
  const shared = { figures, currency, window, costMasked, layout: "card" as const };
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
      <RoiFigure {...shared} metric="hours" caption={countSubtitle} />
      <RoiFigure {...shared} metric="net" caption={costSubtitle} />
      <RoiFigure {...shared} metric="review_rate" caption={reviewSubtitle} onClick={review.total > 0 ? onReview : undefined} />
    </div>
  );
}

/**
 * Said on a fleet's value views when its factory reset falls inside the window (#2580): the
 * figures count only what happened from the reset on. Draws nothing without a mark.
 */
export function RoiResetNote({ since }: { since?: string | null }) {
  const { t_i18n, intl } = useFormatter();
  if (!since) return null;
  const date = intl.formatDate(since, { year: "numeric", month: "short", day: "numeric" });
  return (
    <p role="note" className="flex flex-wrap items-center gap-2 content-compact text-default-secondary" data-testid="roi-reset-note">
      <Info className="h-4 w-4" aria-hidden />
      {t_i18n("Measured since {date} (reset)", { date })}
    </p>
  );
}

/** Said when some minutes or rates are catalog defaults, with the way to set them. */
export function RoiEstimatedNote() {
  const { t_i18n } = useFormatter();
  return (
    <p role="note" className="flex flex-wrap items-center gap-2 content-compact text-default-secondary">
      <Info className="h-4 w-4" aria-hidden />
      {t_i18n("Estimated: some minutes or rates are catalog defaults. Set your own to replace them.")}
      <Link to={UNITS_SETTINGS_PATH} className="text-accent hover:underline">
        {t_i18n("Units and rates")}
      </Link>
    </p>
  );
}

/** The glyph of the hidden-totals empty state, from the design system (UX r1 B3). */
const PeopleGlyph = ({ className }: { className?: string }) => <Icon name="users" size={24} className={className} />;

/** The words of the k rule on one agent or fleet (design 12.10, under-k table). @i18n-keys */
const UNDER_K_WORDS = {
  agent: {
    own: "Your use of this agent",
    note: "{k, plural, one {Totals are shown once {shown} person uses this agent in the period.} other {Totals are shown once {shown} people use this agent in the period.}}",
    hidden: "{k, plural, one {Fewer than {shown} person used this agent in the period: totals are hidden.} other {Fewer than {shown} people used this agent in the period: totals are hidden.}}",
  },
  fleet: {
    own: "Your use of this fleet",
    note: "{k, plural, one {Totals are shown once {shown} person uses this fleet in the period.} other {Totals are shown once {shown} people use this fleet in the period.}}",
    hidden: "{k, plural, one {Fewer than {shown} person used this fleet in the period: totals are hidden.} other {Fewer than {shown} people used this fleet in the period: totals are hidden.}}",
  },
} as const satisfies Record<"agent" | "fleet", { own: string; note: string; hidden: string }>;

/**
 * An agent or fleet used by fewer than k people in the window (#12077 slice L6b3, design 12.5):
 * the heading of the viewer's own share and the k caption when the viewer used it (the hero
 * under it draws their own facts), else the hidden-totals empty state and no hero.
 */
export function RoiUnderK({ kind, k, viewerUsed }: { kind: "agent" | "fleet"; k: number; viewerUsed: boolean }) {
  const { t_i18n } = useFormatter();
  const { locale } = useIntl();
  const words = UNDER_K_WORDS[kind];
  // The plural reads k, {shown} draws it compact (#10770).
  const values = { k, shown: formatCount(locale, k) };
  if (!viewerUsed) {
    return (
      <div data-testid="roi-under-k">
        <EmptyState icon={PeopleGlyph} title={t_i18n(words.hidden, values)} />
      </div>
    );
  }
  return (
    <div className="space-y-1" data-testid="roi-under-k">
      <h3 className="content-base font-semibold">{t_i18n(words.own)}</h3>
      <p role="note" className="flex flex-wrap items-center gap-2 content-compact text-default-secondary">
        <Icon name="info" size={16} aria-hidden />
        {t_i18n(words.note, values)}
      </p>
    </div>
  );
}

/**
 * How the figures are computed, with the report's numbers (the formula per unit, the totals,
 * the review count and the rejected runs left out; design standards s6, UX #9810 r3 B2), then
 * the assumptions behind them, in the reader's language; nothing when there is neither.
 */
export function RoiAssumptions({ facts, computation = [] }: { facts: RoiAssumptionFact[]; computation?: RoiComputationStep[] }) {
  const { t_i18n } = useFormatter();
  const { locale } = useIntl();
  if (facts.length === 0 && computation.length === 0) return null;
  // Every figure of a line is compact (#10770: "47K units ... = $1.2M"); the line's exact form is its hover.
  const compact: LineFormat = {
    count: (value) => formatCount(locale, value),
    money: (value, currency) => formatCurrency(locale, currency, value),
  };
  const exact: LineFormat = {
    count: (value) => exactCount(locale, value),
    money: (value, currency) => exactCurrency(locale, currency, value),
  };
  const stepLine = (step: RoiComputationStep, n: LineFormat): string => {
    switch (step.kind) {
      case "saving":
        return t_i18n(
          "{name}: {units, plural, one {{unitsShown} unit} other {{unitsShown} units}}, {manual} manual min minus {review} review min = {saved} min saved, at {rate} per hour = {value}",
          {
            name: step.name,
            units: step.units,
            unitsShown: n.count(step.units),
            manual: n.count(step.manual_min),
            review: n.count(step.review_min),
            saved: n.count(step.minutes_saved),
            rate: n.money(step.rate, step.currency),
            value: n.money(step.value, step.currency),
          },
        );
      case "total":
        return t_i18n("Hours saved: {minutes} min = {hours} h. Net value: {value} minus model cost {cost} = {net}", {
          minutes: n.count(step.minutes_saved),
          hours: n.count(step.hours_saved),
          value: n.money(step.value, step.currency),
          cost: step.llm_cost === null ? t_i18n("Hidden") : n.money(step.llm_cost, step.currency),
          net: step.net_value === null ? t_i18n("Hidden") : n.money(step.net_value, step.currency),
        });
      case "review":
        return t_i18n("Review rate: {reviewed} reviewed of {total, plural, one {{shown} record} other {{shown} records}}", {
          reviewed: n.count(step.reviewed),
          total: step.total,
          shown: n.count(step.total),
        });
      case "rejected":
        return t_i18n(
          "{count, plural, one {{shown} rejected run counts no time saved, only its cost.} other {{shown} rejected runs count no time saved, only their cost.}}",
          { count: step.count, shown: n.count(step.count) },
        );
      case "source":
        // One By source row of the ledger: the lines sum to the hero, chat included (#12077 L6b3 UX r1 B1).
        return t_i18n("{source}: {hours} h = {value}", {
          source: step.source in SOURCE_NAMES ? t_i18n(SOURCE_NAMES[step.source as keyof typeof SOURCE_NAMES]) : step.source,
          hours: n.count(step.hours),
          value: n.money(step.value, step.currency),
        });
    }
  };
  const figureLabel = (label: string) => (label in FIGURE_LABELS ? t_i18n(FIGURE_LABELS[label as keyof typeof FIGURE_LABELS]) : label);
  const factLine = (fact: RoiAssumptionFact): string => {
    switch (fact.kind) {
      case "unit":
        return t_i18n("{name}: {manual} manual min, {review} review min per unit ({label})", {
          name: fact.name,
          manual: formatCount(locale, fact.manual_min),
          review: formatCount(locale, fact.review_min),
          label: figureLabel(fact.label),
        });
      case "rate":
        return t_i18n("{role} rate: {amount} per hour ({label})", {
          role: fact.role,
          amount: formatCurrency(locale, fact.currency, fact.amount),
          label: figureLabel(fact.label),
        });
      case "measured_review":
        return t_i18n("Review minutes: measured where recorded, the baseline otherwise.");
      case "unknown_units":
        return t_i18n("No baseline for {units}: cost counted, no time saved.", { units: fact.units.join(", ") });
    }
  };
  return (
    <Card className="space-y-2 p-4">
      {computation.length > 0 && (
        <>
          <p className="flex items-center gap-2 content-compact font-medium">
            <Gauge className="h-4 w-4 text-default-secondary" aria-hidden />
            {t_i18n("How this is computed")}
          </p>
          <ul className="list-disc space-y-1 pl-5 content-compact text-default-secondary" data-testid="roi-computation">
            {computation.map((step, index) => (
              <li key={index}>
                <CompactFigure className="whitespace-normal" exact={stepLine(step, exact)}>
                  {stepLine(step, compact)}
                </CompactFigure>
              </li>
            ))}
          </ul>
        </>
      )}
      {facts.length > 0 && (
        <>
          <p className="flex items-center gap-2 content-compact font-medium">
            <Info className="h-4 w-4 text-default-secondary" aria-hidden />
            {t_i18n("Assumptions")}
          </p>
          <ul className="list-disc space-y-1 pl-5 content-compact text-default-secondary">
            {facts.map((fact, index) => (
              <li key={index} title={fact.kind === "rate" ? fact.source : undefined}>
                {factLine(fact)}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

/** The loading state of a report view: its KPI cards and its table. */
export function RoiReportSkeleton() {
  return (
    <>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Skeleton className="h-28 w-full rounded-sm" />
        <Skeleton className="h-28 w-full rounded-sm" />
        <Skeleton className="h-28 w-full rounded-sm" />
      </div>
      <Skeleton className="h-64 w-full rounded-sm" />
    </>
  );
}
