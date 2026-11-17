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
 * `ringGauge`: a resilience on a segmented ring, as OpenAEV draws its
 * resilience gauge (`ResilienceGaugeWidget.tsx`; reference, OpenAEV section 14).
 *
 * The first outcome's checks - succeeded, failed, pending, those with a count -
 * as arcs of a thin ring from the top with small gaps; the share of resolved
 * checks that succeeded in the centre, coloured by its band, "Resilience"
 * under it; a legend of the statuses with their counts; the outcome's glyph in
 * the corner. A click on the ring says the outcome; one on a legend entry,
 * that status.
 */
import { useFormatter } from "@/i18n";
import { useColorMode } from "@/hooks/useColorMode";
import type { WidgetOutcomeCounts, WidgetOutcomeStatus } from "@/types";

import { RING, alpha, resilience, resilienceAccent, ringSegments, statusColor } from "../viz/outcomes";
import { clickable, useChartEvents } from "./common";
import { OutcomeIcon } from "./outcome-common";
import { useCountUp } from "./outcome-hooks";

const GEOLOGICA = "var(--font-sans-geologica)";
const EASE = "1.1s cubic-bezier(0.22, 1, 0.36, 1)";

/** A status's legend word. @i18n-keys */
const STATUS_LABELS = { success: "Success", failed: "Failed", pending: "Pending" } as const;
const STATUS_KEYS: Record<WidgetOutcomeStatus, string> = { success: "SUCCESS", failed: "FAILED", pending: "PENDING" };

export default function RingGauge({ counts, icon }: { counts: WidgetOutcomeCounts & { key?: string }; icon?: string }) {
  const { t_i18n } = useFormatter();
  const mode = useColorMode();
  const { onSelect } = useChartEvents();
  const value = resilience(counts);
  const accent = resilienceAccent(value, mode);
  const animated = useCountUp(value, 1200);
  const c = RING.size / 2;
  const name = counts.key ?? counts.label;
  const legend = (["success", "failed", "pending"] as const).filter((status) => status !== "pending" || counts.pending > 0);
  return (
    <div className="relative flex h-full flex-col items-center justify-between">
      {icon && (
        <span
          className="absolute right-0 top-0 z-[1] flex size-[22px] items-center justify-center rounded-[4px]"
          style={{ color: accent, background: alpha(accent, 0.14) }}
          data-testid="ring-gauge.glyph"
        >
          <OutcomeIcon name={icon} size={14} />
        </span>
      )}
      <div className="relative flex min-h-0 w-full flex-1 items-center justify-center">
        <svg
          viewBox={`0 0 ${RING.size} ${RING.size}`}
          style={{ maxHeight: "100%", maxWidth: "100%", overflow: "visible" }}
          role="img"
          aria-label={t_i18n("{label}: {rate} resilience", { label: counts.label, rate: `${value}%` })}
          {...clickable(onSelect, { name, value: "SUCCESS,FAILED,PENDING" })}
        >
          <title>{t_i18n("click to investigate")}</title>
          <circle cx={c} cy={c} r={RING.radius} fill="none" stroke={mode === "dark" ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.06)"} strokeWidth={RING.stroke} />
          {ringSegments(counts, mode).map((segment) => (
            <circle
              key={segment.status}
              data-status={segment.status}
              cx={c}
              cy={c}
              r={RING.radius}
              fill="none"
              stroke={segment.color}
              strokeWidth={RING.stroke}
              strokeLinecap="round"
              strokeDasharray={segment.dasharray}
              strokeDashoffset={segment.offset}
              transform={`rotate(-90 ${c} ${c})`}
              style={{ transition: `stroke-dasharray ${EASE}, stroke-dashoffset ${EASE}`, filter: `drop-shadow(0 0 2px ${alpha(segment.color, 0.5)})` }}
            />
          ))}
          <text x={c} y={c - 6} textAnchor="middle" dominantBaseline="central" fill={accent} style={{ fontFamily: GEOLOGICA, fontSize: 30, fontWeight: 500 }}>
            {`${Math.round(animated)}%`}
          </text>
          <text x={c} y={c + 16} textAnchor="middle" dominantBaseline="central" fill="var(--text-default-secondary)" style={{ fontSize: 10, letterSpacing: "0.16em", textTransform: "uppercase" }}>
            {t_i18n("Resilience")}
          </text>
        </svg>
      </div>
      <div className="flex flex-wrap justify-center pb-1" style={{ gap: 14 }}>
        {legend.map((status) => {
          const color = statusColor(status, mode);
          return (
            <button
              key={status}
              type="button"
              className="flex items-center gap-1 rounded-[3px] px-1 transition-colors"
              onMouseEnter={(event) => (event.currentTarget.style.background = alpha(color, 0.15))}
              onMouseLeave={(event) => (event.currentTarget.style.background = "")}
              onClick={() => onSelect?.({ name, value: STATUS_KEYS[status] })}
            >
              <span className="size-1.5 rounded-full" style={{ background: color }} />
              <span className="text-default-secondary" style={{ fontSize: 10.5 }}>
                {t_i18n(STATUS_LABELS[status])}
              </span>
              <span className="text-default-primary" style={{ fontFamily: GEOLOGICA, fontSize: 11, fontWeight: 600 }}>
                {counts[status]}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
