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
 * The single value's `difference` and `tile` styles (`viz/value.ts`).
 *
 * The figure is the visualization's value (`majorValue`, a number source's
 * value by default), its change its trend (`trendValue`); a number source
 * adds the value it is compared with (`previous`), over which period
 * (`period`) and its icon, which these styles draw.
 */
import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowRight, ArrowUp, Info, TrendingDown, TrendingUp, MoveRight } from "lucide-react";

import { DeckIcon } from "@/components/presentation/DeckIcon";
import { Tip } from "@/components/ui/tooltip";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { WidgetNumberData } from "@/types";

import { InHeader } from "../header-slot";
import { chipStyle, DIRECTION_TONES, directionOf, tileStyle, trendLabel, wholeDifference, type ValueStyle } from "../viz/value";
import { resolveColor } from "./colors";
import { clickable, useChartEvents } from "./common";
import { optionValue, toNumber, type DosScope } from "./dynamic-options";
import { COUNT_UP_MS, countUpValue, formatNamedNumber } from "./formats";

/** The period a change is measured over. @i18n-keys */
const PERIODS = {
  day: "24 hours",
  interval: "previous period",
} as const;

/** What a number counts, one of it and more. @i18n-keys */
const SUBJECT_ONE = {
  endpoint: "endpoint",
  "expectation-inject": "inject expectation",
  finding: "finding",
  inject: "inject",
  scenario: "scenario",
  simulation: "simulation",
  "vulnerable-endpoint": "vulnerable endpoint",
} as const;
/** @i18n-keys */
const SUBJECT_OTHER = {
  endpoint: "endpoints",
  "expectation-inject": "inject expectations",
  finding: "findings",
  inject: "injects",
  scenario: "scenarios",
  simulation: "simulations",
  "vulnerable-endpoint": "vulnerable endpoints",
} as const;
/** Over what span a change is measured. @i18n-keys */
const SPANS = {
  "1d": "over the last 24 hours",
  "7d": "over the last 7 days",
  "1m": "over the last month",
  "3m": "over the last 3 months",
  "6m": "over the last 6 months",
  "1y": "over the last year",
  all: "since the beginning",
  custom: "over the previous range",
} as const;

const TONE_COLORS = {
  success: "var(--color-feedback-success-primary)",
  danger: "var(--color-feedback-error-primary)",
  neutral: "var(--text-default-secondary)",
} as const;

interface Figures {
  value: number | null;
  change: number | null;
  previous: number | null;
  period: WidgetNumberData["period"];
  icon: WidgetNumberData["icon"];
  subject: WidgetNumberData["subject"];
  span: WidgetNumberData["span"];
}

function figuresOf(shape: string, data: unknown, scope: DosScope): Figures {
  const value = toNumber(optionValue(scope.options?.majorValue, scope));
  const change = toNumber(optionValue(scope.options?.trendValue, scope));
  const number = shape === "number" ? (data as WidgetNumberData) : undefined;
  return {
    value,
    change,
    previous: number?.previous ?? (value !== null && change !== null ? value - change : null),
    period: number?.period,
    icon: number?.icon,
    subject: number?.subject && Object.prototype.hasOwnProperty.call(SUBJECT_ONE, number.subject) ? number.subject : undefined,
    span: number?.span && Object.prototype.hasOwnProperty.call(SPANS, number.span) ? number.span : undefined,
  };
}

/** A figure counting up from the one shown before (from zero at first), easing out; at once when motion is reduced. */
function useCountUp(target: number | null): number | null {
  const [shown, setShown] = useState<number | null>(target === null ? null : 0);
  const from = useRef(0);
  useEffect(() => {
    if (target === null) {
      setShown(null);
      return;
    }
    const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const start = from.current;
    from.current = target;
    if (reduced || typeof requestAnimationFrame !== "function") {
      setShown(target);
      return;
    }
    const began = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const elapsed = now - began;
      setShown(countUpValue(start, target, elapsed));
      if (elapsed < COUNT_UP_MS) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target]);
  return shown;
}

/** A large figure under its change over its period, with an arrow - beside what it counts, when it names it (OpenCTI's `WidgetNumber`). */
function Difference({ figures, label }: { figures: Figures; label?: string }) {
  const { t_i18n, locale } = useFormatter();
  const direction = directionOf(figures.change);
  const Arrow = direction === "up" ? ArrowUp : direction === "down" ? ArrowDown : ArrowRight;
  const change = figures.change !== null && (
    <span
      className="flex shrink-0 items-center gap-0.5 whitespace-nowrap text-xs"
      style={{ color: TONE_COLORS[DIRECTION_TONES[direction]] }}
      data-direction={direction}
    >
      <Arrow className="h-[13px] w-[13px]" aria-hidden="true" />
      <span>{wholeDifference(figures.change)}</span>
      {figures.period && <span>({t_i18n(PERIODS[figures.period])})</span>}
    </span>
  );
  return (
    <div className="flex h-full min-h-[72px] flex-col justify-between gap-2" data-value-style="difference">
      {label ? (
        <span className="flex items-start gap-2">
          <span className="min-w-0 text-[0.8rem] leading-[1.2rem] text-default-secondary" data-value-label="">
            {label}
          </span>
          {change}
        </span>
      ) : (
        change
      )}
      <span className="text-[32px] font-semibold leading-none tabular-nums text-default-primary" data-value="">
        {formatNamedNumber(figures.value, "si", locale)}
      </span>
    </div>
  );
}

/** The change said in a sentence: "+3 findings over the last 3 months (1 previously)" - what is counted and
 *  the span when the data names them, the previous value but over all time. */
function useChangeSentence(figures: Figures): string | null {
  const { t_i18n } = useFormatter();
  const numbers = useNumberFormat();
  if (figures.change === null || (!figures.subject && !figures.span)) return null;
  const one = Math.abs(figures.change) <= 1;
  const subject = figures.subject ? t_i18n(one ? SUBJECT_ONE[figures.subject] : SUBJECT_OTHER[figures.subject]) : "";
  const span = figures.span ? t_i18n(SPANS[figures.span]) : "";
  const said = t_i18n("{change} {subject} {span}", { change: trendLabel(figures.change), subject, span }).replace(/\s+/g, " ").trim();
  const previous = figures.previous !== null && figures.span !== "all" ? ` ${t_i18n("({count} previously)", { count: numbers.count(figures.previous) })}` : "";
  return `${said}${previous}`;
}

/** An icon tile, a figure that counts up, a trend chip, and the change in a sentence behind an info sign: beside
 *  them, or in the widget's header (`infoInHeader`, in the content's accent, which the header does not take). */
function Tile({ figures, infoInHeader, accent }: { figures: Figures; infoInHeader: boolean; accent?: string }) {
  const { t_i18n } = useFormatter();
  const exact = useNumberFormat();
  const { onSelect } = useChartEvents();
  const shown = useCountUp(figures.value);
  const color = resolveColor(figures.icon?.color) ?? "var(--color-accent)";
  const direction = directionOf(figures.change);
  const Trend = direction === "up" ? TrendingUp : direction === "down" ? TrendingDown : MoveRight;
  const trendColor = direction === "flat" ? "var(--text-default-disabled)" : TONE_COLORS[DIRECTION_TONES[direction]];
  const click = clickable(onSelect, { name: "value", value: String(figures.value ?? "") });
  const sentence = useChangeSentence(figures);
  return (
    <div className="flex h-full items-center gap-3 overflow-hidden pb-2" data-value-style="tile">
      <span
        className="flex h-[42px] w-[42px] shrink-0 items-center justify-center rounded-sm"
        style={{ color, ...tileStyle(color) }}
        data-tile-icon={figures.icon?.name ?? "hash"}
      >
        <DeckIcon name={figures.icon?.name ?? "hash"} size={24} />
      </span>
      <span
        {...click}
        className={cn("inline-flex h-9 min-w-0 items-center rounded-sm px-4 text-sm font-semibold tabular-nums text-accent hover:bg-[color-mix(in_srgb,var(--color-accent)_10%,transparent)]", click.className)}
        title={figures.value === null ? undefined : exact.exactCount(figures.value)}
        data-value=""
      >
        {shown === null ? "-" : exact.count(Math.round(shown))}
      </span>
      {figures.change !== null && (
        <span
          className="inline-flex h-[22px] shrink-0 items-center gap-1 rounded-sm px-1.5 font-heading text-xs font-semibold leading-none"
          style={chipStyle(trendColor)}
          title={figures.previous === null ? undefined : t_i18n("({count} previously)", { count: exact.exactCount(figures.previous) })}
          data-direction={direction}
        >
          <Trend className="h-3.5 w-3.5" aria-hidden="true" />
          {trendLabel(figures.change)}
        </span>
      )}
      {sentence &&
        (infoInHeader ? (
          <InHeader>
            <ChangeInfo sentence={sentence} color={accent} />
          </InHeader>
        ) : (
          <ChangeInfo sentence={sentence} />
        ))}
    </div>
  );
}

function ChangeInfo({ sentence, color }: { sentence: string; color?: string }) {
  return (
    <Tip content={sentence}>
      <span className="inline-flex shrink-0 text-accent" style={color ? { color } : undefined} tabIndex={0} aria-label={sentence} data-value-info="">
        <Info className="h-4 w-4" aria-hidden="true" />
      </span>
    </Tip>
  );
}

export default function ValueStyleView({ style, shape, data, scope }: { style: Exclude<ValueStyle, "standard">; shape: string; data: unknown; scope: DosScope }) {
  const figures = figuresOf(shape, data, scope);
  const label = typeof scope.options?.topLabel === "string" && scope.options.topLabel.trim() ? scope.options.topLabel : undefined;
  if (style === "difference") return <Difference figures={figures} label={label} />;
  return <Tile figures={figures} infoInHeader={scope.options?.valueInfo === "header"} accent={resolveColor(scope.options?.accentColor)} />;
}
