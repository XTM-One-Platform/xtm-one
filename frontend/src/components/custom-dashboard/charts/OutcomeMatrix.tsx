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
 * `outcomeMatrix`: labels in columns by group, one chain at a time, as OpenAEV
 * draws its security coverage (`SecurityCoverageContent.tsx`,
 * `KillChainPhaseColumn.tsx`, `AttackPatternBox.tsx`,
 * `components/ColoredPercentageRate.tsx`; reference, OpenAEV section 10).
 *
 * A header: the chain picked, All / Covered / Gaps with their counts, the
 * chain's coverage (labels with an outcome among its labels) with a meter, and
 * the success-rate legend. Then a 170 px column per group of the chain, every
 * label of the group sorted by name, as a box: its code and its successes out
 * of its resolved checks, its name, a meter, its accent by its success rate,
 * muted while untested. A click on a box says the label and every id rolled
 * up into it. Fullscreen is the dashboard frame's own "Expand".
 */
import { useState, type CSSProperties, type ReactNode } from "react";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tip } from "@/components/ui/tooltip";
import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import type { WidgetOutcomesData } from "@/types";

import { MATRIX_COLUMN, SEMANTIC, SUCCESS_LEGEND, alpha, coverageAccent, matrixModel, type CoverageFilter, type MatrixCell } from "../viz/outcomes";
import { useChartEvents } from "./common";
import { OutcomeEmpty } from "./outcome-common";

const GEOLOGICA = "var(--font-sans-geologica)";
const OVERLINE: CSSProperties = { fontFamily: GEOLOGICA, fontWeight: 600, fontSize: 10, letterSpacing: "0.12em", textTransform: "uppercase", lineHeight: 1 };
/** The toggle group's border, and the colour of an option not picked (Material's `action.active`). */
const TOGGLE = { dark: { border: "#2B3447", idle: "rgba(255,255,255,0.56)" }, light: { border: "#D2D2D2", idle: "rgba(0,0,0,0.54)" } } as const;

function Overline({ children }: { children: ReactNode }) {
  return (
    <span className="text-default-secondary" style={OVERLINE}>
      {children}
    </span>
  );
}

/** The success-rate legend: four bands side by side, the best first. */
export function RateLegend({ className }: { className?: string }) {
  return (
    <div className={className} style={{ display: "flex" }} data-testid="outcome.rate-legend">
      {SUCCESS_LEGEND.map(({ label, color }) => (
        <span key={label} style={{ color: "#ffffff", backgroundColor: color, padding: "4px 8px", fontSize: "0.8rem", lineHeight: "1.2rem" }}>
          {label}
        </span>
      ))}
    </div>
  );
}

/** One label as a box: its code and count, its name, a meter, its accent by its success rate; muted untested. */
export function OutcomeBox({ cell, width, onClick }: { cell: MatrixCell; width?: number; onClick?: () => void }) {
  const mode = useColorMode();
  const dark = mode === "dark";
  const accent = coverageAccent(cell.tested ? cell.rate : null, mode);
  const box = (
    <div
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={onClick ? (event) => event.key === "Enter" && onClick() : undefined}
      data-testid="outcome.box"
      data-tested={cell.tested ? "true" : "false"}
      className={
        onClick
          ? "relative cursor-pointer overflow-hidden rounded-[4px] border transition-[transform,box-shadow,border-color] duration-150 hover:-translate-y-px hover:[border-color:var(--box-hover)] hover:[box-shadow:var(--box-shadow)]"
          : "relative overflow-hidden rounded-[4px] border"
      }
      style={{
        width,
        padding: "5px 8px 5px 10px",
        background: cell.tested ? `linear-gradient(90deg, ${alpha(accent, dark ? 0.22 : 0.16)}, ${alpha(accent, 0.04)})` : dark ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.03)",
        borderColor: cell.tested ? alpha(accent, 0.4) : dark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)",
        ["--box-hover" as string]: alpha(accent, 0.7),
        ["--box-shadow" as string]: `0 2px 10px ${alpha(accent, 0.25)}`,
      }}
    >
      <span className="absolute inset-y-0 left-0 w-[3px]" style={{ background: cell.tested ? accent : "transparent" }} />
      <div className="flex items-baseline justify-between gap-1.5">
        <span style={{ fontFamily: GEOLOGICA, fontSize: 10, fontWeight: 700, letterSpacing: "0.04em", color: cell.tested ? accent : "var(--text-default-secondary)" }}>
          {cell.code}
        </span>
        {cell.tested && (
          <span className="text-default-primary" style={{ fontSize: 10.5, fontWeight: 600 }}>
            {`${cell.successCount}/${cell.total}`}
          </span>
        )}
      </div>
      <div
        className="line-clamp-2 break-words"
        style={{ fontSize: 11, lineHeight: 1.25, minHeight: 28, color: cell.tested ? "var(--text-default-primary)" : "var(--text-default-secondary)" }}
      >
        {cell.label}
      </div>
      <div className="mt-1 h-[3px] overflow-hidden rounded-[2px]" style={{ background: dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)" }}>
        <div
          className="h-full rounded-[2px]"
          data-testid="outcome.meter"
          style={{ width: `${cell.tested ? Math.round((cell.rate ?? 0) * 100) : 0}%`, background: accent, transition: "width 0.6s cubic-bezier(0.22, 1, 0.36, 1)" }}
        />
      </div>
    </div>
  );
  return (
    <Tip content={cell.code ? `${cell.code} - ${cell.label}` : cell.label} side="top">
      {box}
    </Tip>
  );
}

/** The scope's toggle group: All, Covered and Gaps with their counts. */
function ScopeToggle({ value, covered, gaps, onChange }: { value: CoverageFilter; covered: number; gaps: number; onChange: (value: CoverageFilter) => void }) {
  const { t_i18n } = useFormatter();
  const mode = useColorMode();
  const primary = SEMANTIC[mode].primary;
  const options: { key: CoverageFilter; label: string; count?: number }[] = [
    { key: "all", label: t_i18n("All") },
    { key: "covered", label: t_i18n("Covered"), count: covered },
    { key: "gaps", label: t_i18n("Gaps"), count: gaps },
  ];
  return (
    <div role="group" aria-label={t_i18n("Techniques")} className="inline-flex">
      {options.map((option, index) => {
        const picked = option.key === value;
        return (
          <button
            key={option.key}
            type="button"
            aria-pressed={picked}
            onClick={() => onChange(option.key)}
            className="inline-flex h-9 items-center border font-medium transition-colors hover:[background:var(--toggle-hover)]"
            style={{
              padding: "2px 10px",
              fontSize: "0.875rem",
              borderColor: TOGGLE[mode].border,
              marginLeft: index > 0 ? -1 : 0,
              borderRadius: index === 0 ? "4px 0 0 4px" : index === options.length - 1 ? "0 4px 4px 0" : 0,
              color: picked ? "var(--text-default-primary)" : TOGGLE[mode].idle,
              background: picked ? alpha(primary, 0.25) : undefined,
              ["--toggle-hover" as string]: picked ? alpha(primary, 0.25) : alpha(primary, 0.15),
            }}
          >
            {option.label}
            {option.count !== undefined && <span className="ml-1.5 text-default-secondary">{option.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

export default function OutcomeMatrix({ data, height }: { data: WidgetOutcomesData; height: number }) {
  const { t_i18n } = useFormatter();
  const mode = useColorMode();
  const { onSelect } = useChartEvents();
  const [chain, setChain] = useState<string | null>(null);
  const [filter, setFilter] = useState<CoverageFilter>("all");
  const model = matrixModel(data, chain, filter);
  const accent = coverageAccent(model.total > 0 ? model.covered / model.total : null, mode);
  const primary = SEMANTIC[mode].primary;
  const named = model.chains.filter((c) => c.id !== "");
  return (
    <div className="flex h-full min-h-0 flex-col" style={{ maxHeight: height }}>
      <header
        className="mb-4 flex flex-wrap items-center justify-between gap-4 rounded-[4px] border"
        style={{
          padding: "12px 16px",
          borderColor: mode === "dark" ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.12)",
          background: `linear-gradient(135deg, ${alpha(primary, 0.06)} 0%, ${alpha("var(--bg-elevation-default-layer-1)", 0.4)} 60%)`,
        }}
      >
        <div className="flex flex-wrap items-end gap-4">
          {named.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <Overline>{t_i18n("Kill chain")}</Overline>
              <Select value={model.chain ?? ""} onValueChange={setChain}>
                <SelectTrigger aria-label={t_i18n("Kill chain")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {named.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.label || c.id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          <div className="flex flex-col gap-1.5">
            <Overline>{t_i18n("Techniques")}</Overline>
            <ScopeToggle value={filter} covered={model.covered} gaps={model.gaps} onChange={setFilter} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-6">
          <div className="flex min-w-[132px] flex-col gap-1.5">
            <Overline>{t_i18n("Coverage")}</Overline>
            <div className="flex items-baseline gap-1.5">
              <span style={{ fontFamily: GEOLOGICA, fontWeight: 600, fontSize: 18, lineHeight: 1, color: accent }}>{`${model.percent}%`}</span>
              <span className="text-default-secondary" style={{ fontSize: "0.75rem" }}>
                {t_i18n("{covered}/{total} tested", { covered: model.covered, total: model.total })}
              </span>
            </div>
            <div className="h-1 overflow-hidden rounded-[2px]" style={{ background: "color-mix(in srgb, var(--text-default-primary) 8%, transparent)" }}>
              <div className="h-full rounded-[2px]" style={{ width: `${model.percent}%`, background: accent }} data-testid="outcome-matrix.coverage" />
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Overline>{t_i18n("Success rate")}</Overline>
            <RateLegend />
          </div>
        </div>
      </header>
      <div className="flex min-h-0 flex-1 gap-2 overflow-auto pr-2">
        {model.empty ? (
          <div className="flex-1">
            <OutcomeEmpty message={t_i18n("No data to display")} />
          </div>
        ) : (
          model.columns.map((column) => (
            <section key={column.id} aria-label={column.label}>
              <Tip content={column.label}>
                <h5
                  className="mb-4 block truncate"
                  style={{ fontFamily: GEOLOGICA, fontWeight: 700, fontSize: 16, maxWidth: MATRIX_COLUMN }}
                >
                  {column.label}
                </h5>
              </Tip>
              <div className="grid gap-2 pb-2" style={{ width: MATRIX_COLUMN }}>
                {column.cells.map((cell) => (
                  <OutcomeBox
                    key={cell.id}
                    cell={cell}
                    onClick={onSelect ? () => onSelect({ name: cell.label, value: cell.id, row: { id: cell.id, code: cell.code, ids: [cell.id, ...cell.subIds].join(",") } }) : undefined}
                  />
                ))}
              </div>
            </section>
          ))
        )}
      </div>
    </div>
  );
}
