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
 * "How this is computed" (#9836 R4b, design dev-docs/roi-outcomes.md 10.6):
 * the 28 px info button beside every ROI figure, and the popover it opens
 * with the formula in words and with the report's own numbers, each input
 * with its label (Default, Customized, Assumption, Published estimate,
 * Measured) and its source, what was counted and what was not. The numbers
 * are the report's, written per unit, so the figure is reproduced by hand.
 * Built on the shared InfoPopover (UX r1 B6): Escape, focus, scroll and flip
 * are the ones every info bubble has.
 */
import { VALUE_METHODOLOGY_PATH } from "@/lib/task-routes";
import { BookOpen, Info } from "lucide-react";
import { useMemo } from "react";
import { useIntl } from "react-intl";
import { Link, useInRouterContext } from "react-router-dom";
import { InfoPopover } from "@/components/ui/info-popover";
import type { RoiWindow } from "@/hooks/api/useRoiReportApi";
import { useFormatter } from "@/i18n";
import { withBasePath } from "@/lib/base-path";
import { exactCount, exactCurrency, formatCount } from "@/lib/number-format";
import { RATE_ROLE_NAMES, roiComputation, shownLabel, SHOWN_LABEL_NAMES, SOURCE_NAMES, type RoiFigures, type RoiMetric } from "@/lib/roi-figure";

/** The methodology page every popover links to (#9836 R7b, design 10.6). */
const ROI_METHODOLOGY_PATH = VALUE_METHODOLOGY_PATH;

const METHODOLOGY_LINK_CLASS =
  "mt-3 inline-flex h-7 items-center gap-1 content-compact font-medium text-accent underline-offset-2 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus";

/** The popover's width (design 10.6: 480 to 560 px), kept inside the viewport at 1024. */
const POPOVER_WIDTH = 520;

/** The window names of the popover's header. @i18n-keys */
const WINDOW_NAMES = {
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
  quarter: "This quarter",
  all: "All time",
} as const satisfies Record<RoiWindow, string>;

interface RoiHowComputedProps {
  figures: RoiFigures;
  metric: RoiMetric;
  currency: string;
  window: RoiWindow;
  costMasked?: boolean;
}

export function RoiHowComputed({ figures, metric, currency, window, costMasked }: RoiHowComputedProps) {
  const { t_i18n } = useFormatter();
  const { locale } = useIntl();
  const c = useMemo(() => roiComputation(figures, costMasked), [figures, costMasked]);

  // The formula lines are written exact so the figure is reproduced by hand (design 10.6),
  // through the shared exact forms (#10770); the counted lines below are compact.
  const money = (amount: number) => exactCurrency(locale, currency, amount);
  const num = (value: number) => exactCount(locale, value);
  const roleName = (role: string) => (role in RATE_ROLE_NAMES ? t_i18n(RATE_ROLE_NAMES[role as keyof typeof RATE_ROLE_NAMES]) : role);
  const hidden = t_i18n("Hidden");
  const sourceName = (source: string) => (source in SOURCE_NAMES ? t_i18n(SOURCE_NAMES[source as keyof typeof SOURCE_NAMES]) : source);

  const lines: { formula: string; numbers: string }[] = [
    {
      formula:
        c.sources.length > 0 ? t_i18n("Value = the sum of its sources, chat included") : t_i18n("Value = units x (manual minutes - review minutes) x hourly rate"),
      // The ledger's hero is its By source lines summed, chat included (#12077 L6b3 UX r1 B1).
      numbers:
        c.sources.length > 0
          ? c.sources.map((s) => `${sourceName(s.source)} ${money(s.value)}`).join(" + ") + ` = ${money(c.value)}`
          : c.savings.length > 0
          ? c.savings
              .map((s) =>
                t_i18n("{units} x ({manual} - {review}) min per unit x {rate}/h", {
                  units: num(s.units),
                  manual: num(s.manual_per_unit),
                  review: num(s.review_per_unit),
                  rate: money(s.rate),
                }),
              )
              .join(" + ") + ` = ${money(c.value)}`
          : `= ${money(c.value)}`,
    },
    {
      formula: t_i18n("Net value = value - AI cost"),
      numbers: c.cost === null || c.net === null ? `= ${hidden}` : `${money(c.value)} - ${money(c.cost)} = ${money(c.net)}`,
    },
    {
      formula: t_i18n("ROI = net value / AI cost"),
      numbers:
        c.cost === null || c.net === null
          ? `= ${hidden}`
          : c.roi === null
            ? "= -"
            : `${money(c.net)} / ${money(c.cost)} = ${t_i18n("{value} x", { value: num(c.roi) })}`,
    },
  ];

  return (
    <InfoPopover
      label={t_i18n("How this is computed")}
      icon={<Info className="h-3.5 w-3.5" aria-hidden />}
      heading={t_i18n("How this is computed")}
      headingClassName="content-compact"
      headingAside={<span className="content-compact text-default-secondary">{t_i18n(WINDOW_NAMES[window])}</span>}
      width={POPOVER_WIDTH}
      align="end"
      triggerTestId={`roi-how-${metric}`}
      triggerClassName="inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-default-secondary hover:text-default-primary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
      popoverClassName="p-4"
      popoverTestId="roi-how-popover"
    >
            <dl className="space-y-2 content-compact">
              {lines.map((line) => (
                <div key={line.formula}>
                  <dt className="font-medium">{line.formula}</dt>
                  <dd className="text-default-secondary break-words" data-testid="roi-how-numbers">
                    {line.numbers}
                  </dd>
                </div>
              ))}
            </dl>
            {(c.unitFacts.length > 0 || c.rateFacts.length > 0) && (
              <>
                <p className="mt-4 mb-1 content-compact font-semibold">{t_i18n("Inputs")}</p>
                <ul className="space-y-1.5 content-compact">
                  {c.unitFacts.map((f) => (
                    <InputRow
                      key={`unit:${f.unit}`}
                      name={t_i18n("{name}: manual minutes {manual}, review minutes {review}", {
                        name: f.name,
                        manual:
                          f.manual_min_low !== undefined && f.manual_min_high !== undefined && f.manual_min_low !== f.manual_min_high
                            ? t_i18n("{value} ({low} to {high})", { value: num(f.manual_min), low: num(f.manual_min_low), high: num(f.manual_min_high) })
                            : num(f.manual_min),
                        review: num(f.review_min),
                      })}
                      label={t_i18n(SHOWN_LABEL_NAMES[shownLabel(f.label)])}
                      source={f.source}
                    />
                  ))}
                  {c.rateFacts.map((f) => (
                    <InputRow
                      key={`rate:${f.role}`}
                      name={t_i18n("Hourly rate ({role}): {amount}/h", { role: roleName(f.role), amount: money(f.amount) })}
                      label={t_i18n(SHOWN_LABEL_NAMES[shownLabel(f.label)])}
                      source={f.source}
                    />
                  ))}
                  <InputRow
                    name={t_i18n("AI cost: {cost}", { cost: c.cost === null ? hidden : money(c.cost) })}
                    label={t_i18n(SHOWN_LABEL_NAMES.measured)}
                    source={t_i18n("Each model call of this period, chat and other usage included")}
                  />
                </ul>
              </>
            )}
            <p className="mt-4 content-compact text-default-secondary">
              {c.deliverables > 0 && c.workItems > 0
                ? t_i18n(
                    "Counted: {units, plural, one {{unitsShown} unit} other {{unitsShown} units}} from {runs, plural, one {{runsShown} run} other {{runsShown} runs}}, {items, plural, one {{itemsShown} work item} other {{itemsShown} work items}} and {chats, plural, one {{chatsShown} chat deliverable} other {{chatsShown} chat deliverables}}",
                    {
                      units: c.units,
                      unitsShown: formatCount(locale, c.units),
                      runs: c.runs,
                      runsShown: formatCount(locale, c.runs),
                      items: c.workItems,
                      itemsShown: formatCount(locale, c.workItems),
                      chats: c.deliverables,
                      chatsShown: formatCount(locale, c.deliverables),
                    },
                  )
                : c.deliverables > 0
                ? t_i18n(
                    "Counted: {units, plural, one {{unitsShown} unit} other {{unitsShown} units}} from {runs, plural, one {{runsShown} run} other {{runsShown} runs}} and {chats, plural, one {{chatsShown} chat deliverable} other {{chatsShown} chat deliverables}}",
                    {
                      units: c.units,
                      unitsShown: formatCount(locale, c.units),
                      runs: c.runs,
                      runsShown: formatCount(locale, c.runs),
                      chats: c.deliverables,
                      chatsShown: formatCount(locale, c.deliverables),
                    },
                  )
                : c.workItems > 0
                ? t_i18n(
                    "Counted: {units, plural, one {{unitsShown} unit} other {{unitsShown} units}} from {runs, plural, one {{runsShown} run} other {{runsShown} runs}} and {items, plural, one {{itemsShown} work item} other {{itemsShown} work items}}",
                    {
                      units: c.units,
                      unitsShown: formatCount(locale, c.units),
                      runs: c.runs,
                      runsShown: formatCount(locale, c.runs),
                      items: c.workItems,
                      itemsShown: formatCount(locale, c.workItems),
                    },
                  )
                : t_i18n(
                    "Counted: {units, plural, one {{unitsShown} unit} other {{unitsShown} units}} from {runs, plural, one {{runsShown} run} other {{runsShown} runs}}",
                    {
                      units: c.units,
                      unitsShown: formatCount(locale, c.units),
                      runs: c.runs,
                      runsShown: formatCount(locale, c.runs),
                    },
                  )}
            </p>
            <p className="content-compact text-default-secondary">
              {t_i18n(
                "Not counted: {rejected, plural, one {{rejectedShown} rejected} other {{rejectedShown} rejected}}; {unreviewed, plural, one {{unreviewedShown} unreviewed} other {{unreviewedShown} unreviewed}} counted apart",
                {
                  rejected: c.rejected,
                  rejectedShown: formatCount(locale, c.rejected),
                  unreviewed: c.unreviewed,
                  unreviewedShown: formatCount(locale, c.unreviewed),
                },
              )}
            </p>
            <MethodologyLink label={t_i18n("Read the methodology")} />
    </InfoPopover>
  );
}

/** "Read the methodology": a router link in the app, a plain link where no router is mounted (an isolated render). */
function MethodologyLink({ label }: { label: string }) {
  const inRouter = useInRouterContext();
  const content = (
    <>
      <BookOpen className="h-3.5 w-3.5" aria-hidden /> {label}
    </>
  );
  return inRouter ? (
    <Link to={ROI_METHODOLOGY_PATH} className={METHODOLOGY_LINK_CLASS} data-testid="roi-how-methodology">
      {content}
    </Link>
  ) : (
    <a href={withBasePath(ROI_METHODOLOGY_PATH)} className={METHODOLOGY_LINK_CLASS} data-testid="roi-how-methodology">
      {content}
    </a>
  );
}

/** One input: its name and value, its label, then its source written out (UX r1 B3 and A1: readable, never hover-only, 12 px). */
function InputRow({ name, label, source }: { name: string; label: string; source?: string }) {
  return (
    <li className="min-w-0">
      <div className="flex items-baseline justify-between gap-3">
        <span className="min-w-0 break-words">{name}</span>
        <span className="shrink-0 font-medium text-default-secondary">{label}</span>
      </div>
      {source && <span className="block break-words text-default-secondary">{source}</span>}
    </li>
  );
}
