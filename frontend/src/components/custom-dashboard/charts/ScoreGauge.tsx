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
 * `scoreGauge`: an exposure score on a 270° gauge, as OpenAEV draws its
 * exposure score (`ExposureScoreWidget.tsx`; reference, OpenAEV section 13).
 *
 * The share of resolved checks that failed, from the bottom left clockwise
 * over the top: a track, the value arc in the success-to-error gradient with a
 * glow, a needle dot at its tip, a radar sweep turning behind; the score
 * counting up, "/ 100" and the band under it; then the first four labels' own
 * exposure. No click, as in the module.
 */
import { useId } from "react";

import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import type { WidgetOutcomesData } from "@/types";

import { EXPOSURE_BANDS, SCORE_GAUGE, SEMANTIC, exposureBand, exposureScore, scoreBreakdown, scoreGaugeArcs, tint, totalsOf } from "../viz/outcomes";
import { useCountUp, useDrawn, useOutcomeWords } from "./outcome-hooks";

const GEOLOGICA = "var(--font-sans-geologica)";
const EASE = "1.4s cubic-bezier(0.22, 1, 0.36, 1)";

export default function ScoreGauge({ data }: { data: WidgetOutcomesData }) {
  const { t_i18n } = useFormatter();
  const word = useOutcomeWords();
  const mode = useColorMode();
  const gradient = `exposure-gradient-${useId().replace(/:/g, "")}`;
  const overall = exposureScore(totalsOf(data.items));
  const { band, color } = exposureBand(overall, mode);
  const animated = useCountUp(overall, 1400);
  const arcs = scoreGaugeArcs(useDrawn(overall / 100));
  const track = mode === "dark" ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)";
  const theme = SEMANTIC[mode];
  const c = SCORE_GAUGE.size / 2;
  return (
    <div className="flex h-full flex-col items-center justify-between overflow-hidden">
      <div className="relative flex min-h-0 w-full flex-1 items-center justify-center">
        {/* The radar sweep, turning slowly behind the gauge. */}
        <span
          aria-hidden="true"
          className="absolute animate-spin rounded-full motion-reduce:animate-none"
          style={{
            width: SCORE_GAUGE.sweepSize,
            height: SCORE_GAUGE.sweepSize,
            background: `conic-gradient(from 0deg, transparent 0deg, ${tint(color, 13)} 40deg, transparent 80deg)`,
            animationDuration: "6s",
            animationTimingFunction: "linear",
          }}
        />
        <svg
          viewBox={`0 0 ${SCORE_GAUGE.size} ${SCORE_GAUGE.size}`}
          className="relative"
          style={{ maxHeight: "100%", maxWidth: "100%", overflow: "visible" }}
          role="img"
          aria-label={t_i18n("Exposure score {score} out of 100", { score: overall })}
        >
          <defs>
            <linearGradient id={gradient} x1="0%" y1="100%" x2="100%" y2="0%">
              <stop offset="0%" stopColor={theme.success} />
              <stop offset="45%" stopColor={theme.warning} />
              <stop offset="100%" stopColor={theme.error} />
            </linearGradient>
          </defs>
          <path d={arcs.track} fill="none" stroke={track} strokeWidth={SCORE_GAUGE.stroke} strokeLinecap="round" />
          <path
            d={arcs.value}
            fill="none"
            stroke={`url(#${gradient})`}
            strokeWidth={SCORE_GAUGE.stroke}
            strokeLinecap="round"
            data-testid="score-gauge.value"
            style={{ transition: `all ${EASE}`, filter: `drop-shadow(0 0 6px ${tint(color, 53)})` }}
          />
          <circle cx={arcs.tip.x} cy={arcs.tip.y} r={5} fill={color} style={{ transition: `all ${EASE}`, filter: `drop-shadow(0 0 8px ${color})` }} />
          <text x={c} y={c - 2} textAnchor="middle" fill="var(--text-default-primary)" style={{ fontFamily: GEOLOGICA, fontSize: 44, fontWeight: 600 }}>
            {Math.round(animated)}
          </text>
          <text x={c} y={c + 20} textAnchor="middle" fill="var(--text-default-secondary)" style={{ fontSize: 11, letterSpacing: "0.14em", textTransform: "uppercase" }}>
            {t_i18n("/ 100")}
          </text>
          <text
            x={c}
            y={c + 46}
            textAnchor="middle"
            fill={color}
            style={{ fontFamily: GEOLOGICA, fontSize: 12, fontWeight: 600, letterSpacing: "0.1em", textTransform: "uppercase" }}
          >
            {t_i18n(EXPOSURE_BANDS[band])}
          </text>
        </svg>
      </div>
      <div className="flex w-full gap-3 pb-1">
        {scoreBreakdown(data.items, mode).map(({ item, exposure, color: bucketColor }) => (
          <div key={item.key ?? item.label} className="min-w-0 flex-1">
            <div className="truncate uppercase text-default-secondary" style={{ fontSize: 10, letterSpacing: "0.08em" }}>
              {word(item.label)}
            </div>
            <div className="flex items-center gap-1.5">
              <div className="h-1 flex-1 overflow-hidden rounded-[2px]" style={{ background: track }}>
                <div className="h-full rounded-[2px]" style={{ width: `${exposure}%`, background: bucketColor, transition: `width ${EASE}` }} />
              </div>
              <span style={{ fontFamily: GEOLOGICA, fontSize: 11, fontWeight: 600, color: bucketColor }}>{exposure}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
