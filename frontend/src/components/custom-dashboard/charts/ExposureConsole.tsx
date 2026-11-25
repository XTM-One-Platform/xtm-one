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
 * `exposureConsole`: an exposure score with what stopped each attempt and the
 * calls to act on it, as OpenAEV draws its command center
 * (`command_center/CommandCenterWidget.tsx`, `ExposureConsole.tsx`,
 * `AttackFlow.tsx`; reference, OpenAEV section 12).
 *
 * Three columns:
 * - the orb: the score (the share of resolved checks that failed) on a
 *   gradient ring around a glass sphere, the tools answering the checks
 *   orbiting it (drawn by their type's abbreviation: their logos are not
 *   proxied), and the band's chip; the orb or the chip opens the explanation;
 * - the attack flow (680 x 240): the adversary's attempts, a gate per outcome
 *   with any check (its success rate rising, its successes, a gauge), particles
 *   stopped at the gates or breaching through, and what got through;
 * - the action rail: the data's `actions`, each a link.
 *
 * A click on a gate says its outcome; on the adversary, `all`; on what got
 * through (or the explanation's button), `breach`.
 */
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import { Button } from "@filigran/design-system";
import { Info } from "lucide-react";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Tip } from "@/components/ui/tooltip";
import { useColorMode } from "@/hooks/useColorMode";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";

import type { WidgetOutcome, WidgetOutcomesData } from "@/types";

import type { ColorMode } from "./palettes";
import {
  BAND_DESCRIPTIONS,
  BAND_RANGES,
  EXPOSURE_BANDS,
  FLOW,
  ORB,
  SEMANTIC,
  abbreviate,
  alpha,
  exposureBand,
  exposureScore,
  flowGates,
  orbRing,
  orbitName,
  orbitPositions,
  resilience,
  tint,
  toneColor,
  totalsOf,
  truncateLabel,
  type ExposureBand,
} from "../viz/outcomes";
import { useChartEvents } from "./common";
import { formatNamedNumber } from "./formats";
import { OutcomeIcon } from "./outcome-common";
import { useCountUp, useDrawn, useOutcomeWords } from "./outcome-hooks";
import { dataHref } from "./links";

const GEOLOGICA = "var(--font-sans-geologica)";
const EASE = "1.4s cubic-bezier(0.22, 1, 0.36, 1)";
const compact = (value: number) => formatNamedNumber(value, "compact");

/** Freezes an SVG's animations while the page is hidden, so it never catches up minutes of loops. */
function useVisibilityPause() {
  const ref = useRef<SVGSVGElement | null>(null);
  useEffect(() => {
    const svg = ref.current;
    if (!svg || typeof document === "undefined") return;
    const sync = () => (document.hidden ? svg.pauseAnimations?.() : svg.unpauseAnimations?.());
    document.addEventListener("visibilitychange", sync);
    return () => document.removeEventListener("visibilitychange", sync);
  }, []);
  return ref;
}

/** Enter or Space acts as a click. */
const pressable = (label: string, action: () => void) => ({
  role: "button",
  tabIndex: 0,
  "aria-label": label,
  onClick: action,
  onKeyDown: (event: KeyboardEvent<Element>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      action();
    }
  },
});

// ── The orb ─────────────────────────────────────────────────────────────────

function Orb({ data, score, gaps, validations, mode, onExplain }: { data: WidgetOutcomesData; score: number; gaps: number; validations: number; mode: ColorMode; onExplain: () => void }) {
  const { t_i18n } = useFormatter();
  const ids = useId().replace(/:/g, "");
  const svg = useVisibilityPause();
  const { band, color } = exposureBand(score, mode);
  const theme = SEMANTIC[mode];
  const dark = mode === "dark";
  const animated = useCountUp(score, 1400);
  const ring = orbRing(useDrawn(score / 100));
  const c = ORB.size / 2;
  const orbit = (data.orbit ?? []).slice(0, ORB.maxOrbit);
  const positions = orbitPositions(orbit.length);
  const explain = pressable(t_i18n("How is this score computed?"), onExplain);
  return (
    <div className="relative flex h-full min-h-0 w-full flex-1 flex-col items-center justify-center">
      <div className="relative flex min-h-0 w-full flex-1 items-center justify-center">
        <svg
          ref={svg}
          viewBox={`0 0 ${ORB.size} ${ORB.size}`}
          style={{ maxHeight: "100%", maxWidth: "100%", overflow: "visible", cursor: "pointer" }}
          data-testid="exposure-console.orb"
          {...explain}
        >
          <defs>
            <linearGradient id={`${ids}-score`} x1="0%" y1="100%" x2="100%" y2="0%">
              <stop offset="0%" style={{ stopColor: theme.success }} />
              <stop offset="45%" style={{ stopColor: theme.warning }} />
              <stop offset="100%" style={{ stopColor: theme.error }} />
            </linearGradient>
            <radialGradient id={`${ids}-glass`} cx="38%" cy="30%" r="75%">
              <stop offset="0%" style={{ stopColor: tint(color, 32) }} />
              <stop offset="55%" style={{ stopColor: tint(color, 10) }} />
              <stop offset="100%" style={{ stopColor: alpha(dark ? "#000000" : "#ffffff", 0.06) }} />
            </radialGradient>
            <radialGradient id={`${ids}-gloss`} cx="50%" cy="50%" r="50%">
              <stop offset="0%" style={{ stopColor: alpha("#ffffff", dark ? 0.14 : 0.5) }} />
              <stop offset="70%" style={{ stopColor: alpha("#ffffff", dark ? 0.06 : 0.2) }} />
              <stop offset="100%" style={{ stopColor: alpha("#ffffff", 0) }} />
            </radialGradient>
          </defs>
          <circle cx={c} cy={c} r={ORB.ring - 6} fill={`url(#${ids}-glass)`} style={{ stroke: tint(color, 18) }} strokeWidth={1} />
          <circle cx={c} cy={c} r={ORB.ring - 24} fill="none" stroke={dark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.05)"} strokeWidth={1} />
          <ellipse cx={c} cy={c} rx={ORB.ring - 10} ry={(ORB.ring - 10) / 3} fill="none" style={{ stroke: tint(color, 12) }} strokeWidth={1} />
          <ellipse cx={c - 14} cy={c - 34} rx={40} ry={20} fill={`url(#${ids}-gloss)`} />
          <ellipse cx={c} cy={c} rx={ORB.orbit} ry={ORB.orbit} fill="none" stroke={dark ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.04)"} strokeWidth={1} strokeDasharray="2 6" />
          <g data-testid="exposure-console.orbit">
            <animateTransform attributeName="transform" type="rotate" from={`0 ${c} ${c}`} to={`360 ${c} ${c}`} dur={`${ORB.orbitSeconds}s`} repeatCount="indefinite" />
            {orbit.map((member, i) => {
              const at = positions[i];
              const memberColor = member.color ?? theme.primary;
              return (
                <g key={`${member.label}-${i}`} data-member={member.type ?? ""}>
                  <animateTransform attributeName="transform" type="rotate" from={`360 ${at.x} ${at.y}`} to={`0 ${at.x} ${at.y}`} dur={`${ORB.orbitSeconds}s`} repeatCount="indefinite" />
                  <title>{`${member.label} (${member.type ?? ""})`}</title>
                  <circle cx={at.x} cy={at.y} r={ORB.node} fill={dark ? "#0c1526" : "#f4f8fc"} style={{ stroke: alpha(memberColor, 0.7) }} strokeWidth={1.2} />
                  <circle cx={at.x} cy={at.y} r={ORB.node} style={{ fill: alpha(memberColor, dark ? 0.16 : 0.1) }} />
                  <circle cx={at.x} cy={at.y} r={ORB.node} fill="none" style={{ stroke: alpha(memberColor, 0.35) }} strokeWidth={1}>
                    <animate attributeName="r" values="13;16;13" dur={`${3 + (i % 3)}s`} repeatCount="indefinite" />
                    <animate attributeName="opacity" values="0.5;0;0.5" dur={`${3 + (i % 3)}s`} repeatCount="indefinite" />
                  </circle>
                  <text x={at.x} y={at.y} textAnchor="middle" dominantBaseline="central" style={{ fill: memberColor, fontSize: 10, fontWeight: 700, letterSpacing: "0.04em" }}>
                    {abbreviate(member.type)}
                  </text>
                  <text x={at.x} y={at.y + 21} textAnchor="middle" dominantBaseline="central" fill="var(--text-default-secondary)" style={{ fontSize: 10, letterSpacing: "0.03em" }}>
                    {orbitName(member.label)}
                  </text>
                </g>
              );
            })}
          </g>
          <path d={ring.path} fill="none" stroke={dark ? "rgba(255,255,255,0.07)" : "rgba(0,0,0,0.07)"} strokeWidth={5} strokeLinecap="round" />
          <path
            d={ring.path}
            fill="none"
            stroke={`url(#${ids}-score)`}
            strokeWidth={11}
            strokeLinecap="round"
            strokeDasharray={ring.circumference}
            strokeDashoffset={ring.offset}
            opacity={0.28}
            style={{ transition: `stroke-dashoffset ${EASE}` }}
          />
          <path
            d={ring.path}
            fill="none"
            stroke={`url(#${ids}-score)`}
            strokeWidth={5}
            strokeLinecap="round"
            strokeDasharray={ring.circumference}
            strokeDashoffset={ring.offset}
            data-testid="exposure-console.ring"
            style={{ transition: `stroke-dashoffset ${EASE}` }}
          />
          <g style={{ transform: `rotate(${ring.rotation}deg)`, transformOrigin: `${c}px ${c}px`, transition: `transform ${EASE}` }}>
            <circle cx={ring.marker.x} cy={ring.marker.y} r={8.5} style={{ fill: tint(color, 30) }} />
            <circle cx={ring.marker.x} cy={ring.marker.y} r={4.5} style={{ fill: color }} />
          </g>
          <text x={c} y={c - 6} textAnchor="middle" dominantBaseline="central" fill="var(--text-default-primary)" style={{ fontFamily: GEOLOGICA, fontSize: 52, fontWeight: 500 }}>
            {Math.round(animated)}
          </text>
          <text
            x={c}
            y={c + 28}
            textAnchor="middle"
            dominantBaseline="central"
            style={{ fill: color, fontSize: 10, fontWeight: 600, letterSpacing: "0.24em", textTransform: "uppercase" }}
          >
            {t_i18n("At risk")}
          </text>
        </svg>
      </div>
      <Tip content={t_i18n("Exposure score {score} / 100 - click to understand how it is computed", { score: Math.round(score) })}>
        <span
          className="inline-flex h-[22px] cursor-pointer items-center gap-1.5 rounded-sm border px-2 transition-colors"
          style={{ borderColor: tint(color, 30), background: tint(color, 10) }}
          onMouseEnter={(event) => (event.currentTarget.style.background = tint(color, 20))}
          onMouseLeave={(event) => (event.currentTarget.style.background = tint(color, 10))}
          data-testid="exposure-console.chip"
          {...explain}
        >
          <span className="size-1.5 rounded-full" style={{ background: color, boxShadow: `0 0 6px ${color}` }} />
          <span className="uppercase" style={{ fontSize: 10, letterSpacing: "0.08em", color }}>
            {t_i18n(EXPOSURE_BANDS[band])}
          </span>
          <span className="text-default-secondary" style={{ fontSize: 10 }}>
            {gaps === 0 && validations === 0
              ? t_i18n("No validations yet")
              : gaps === 0
                ? t_i18n("All controls holding")
                : t_i18n("{count} gaps to remediate", { count: compact(gaps) })}
          </span>
        </span>
      </Tip>
    </div>
  );
}

// ── The explanation ─────────────────────────────────────────────────────────

function Explanation({ data, score, gaps, validations, mode, open, onClose, onInvestigate }: {
  data: WidgetOutcomesData; score: number; gaps: number; validations: number; mode: ColorMode; open: boolean; onClose: () => void; onInvestigate?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const exact = useNumberFormat();
  const word = useOutcomeWords();
  const theme = SEMANTIC[mode];
  const { band, color } = exposureBand(score, mode);
  const pillars = data.items.map((item) => ({ item, total: item.success + item.failed })).filter((pillar) => pillar.total > 0);
  const legend = (fill: string, label: string) => (
    <span className="flex items-center gap-1">
      <span className="size-2.5 rounded-full" style={{ background: fill }} />
      <span className="content-compact text-default-secondary">{label}</span>
    </span>
  );
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent size="md" className="max-w-[600px]">
        <DialogTitle className="flex items-center gap-2">
          <Info className="size-5" style={{ color: theme.primary }} aria-hidden="true" />
          {t_i18n("Adversarial exposure score")}
        </DialogTitle>
        <div className="flex max-h-[70vh] flex-col overflow-auto">
          <div className="mb-4 flex items-center gap-4 rounded-[4px] border p-4" style={{ borderColor: tint(color, 30), background: tint(color, 8) }}>
            <span style={{ fontFamily: GEOLOGICA, fontSize: 44, fontWeight: 500, lineHeight: 1, color }}>{Math.round(score)}</span>
            <div>
              <div className="font-semibold uppercase" style={{ letterSpacing: "0.08em", color }}>
                {t_i18n(EXPOSURE_BANDS[band])}
              </div>
              <div className="content-compact text-default-secondary">
                {validations === 0
                  ? t_i18n("No validations have run yet.")
                  : t_i18n("{gaps} of {validations} validations breached your controls.", { gaps: exact.count(gaps), validations: exact.count(validations) })}
              </div>
            </div>
          </div>
          <h4 className="mb-1 content-base font-medium">{t_i18n("What it measures")}</h4>
          <p className="mb-4 content-compact text-default-secondary">
            {t_i18n(
              "The adversarial exposure score is the share of security validations your controls failed to stop. It runs from 0 to 100 and, unlike a resilience score, a HIGHER number is WORSE - it means you are more exposed.",
            )}
          </p>
          <div className="mb-4 rounded-[4px] border p-3 text-center font-mono text-[13px] text-default-primary" style={{ background: mode === "dark" ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.04)", borderColor: mode === "dark" ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.12)" }}>
            {t_i18n("exposure")}
            {" = "}
            <span style={{ color: theme.error }}>{`${exact.count(gaps)} ${t_i18n("breached")}`}</span>
            {" / "}
            <span>{`${exact.count(validations)} ${t_i18n("total")}`}</span>
            {" x 100 = "}
            <span className="font-bold" style={{ color }}>
              {Math.round(score)}
            </span>
          </div>
          <p className="mb-4 content-compact text-default-secondary">
            {t_i18n(
              "Every validation counts equally - there is no per-pillar weighting. Pillars that run more validations therefore weigh more on the overall score. It is the exact inverse of the resilience gauges below: exposure = 100 - overall resilience ({resilience}%).",
              { resilience: resilience(totalsOf(data.items)) },
            )}
          </p>
          {pillars.length > 0 && (
            <>
              <h4 className="mb-1 mt-4 content-base font-medium">{t_i18n("Breakdown by pillar")}</h4>
              {pillars.map(({ item, total }) => {
                const breach = Math.round((item.failed / total) * 100);
                return (
                  <div key={item.key ?? item.label} className="mb-3" data-testid="exposure-console.pillar">
                    <div className="mb-1 flex justify-between">
                      <span className="content-compact font-semibold">{word(item.label)}</span>
                      <span className="content-compact text-default-secondary">
                        {t_i18n("{failed} / {total} breached ({pct}%)", { failed: exact.count(item.failed), total: exact.count(total), pct: breach })}
                      </span>
                    </div>
                    <Tip content={t_i18n("{stopped} stopped - {breached} breached", { stopped: exact.exactCount(item.success), breached: exact.exactCount(item.failed) })}>
                      <div className="flex h-2 overflow-hidden rounded-full" style={{ background: mode === "dark" ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.04)" }}>
                        <div style={{ width: `${100 - breach}%`, background: theme.success }} />
                        <div style={{ width: `${breach}%`, background: theme.error }} />
                      </div>
                    </Tip>
                  </div>
                );
              })}
              <div className="mt-2 flex gap-4">
                {legend(theme.success, t_i18n("Stopped"))}
                {legend(theme.error, t_i18n("Breached"))}
              </div>
            </>
          )}
          <h4 className="mb-1 mt-4 content-base font-medium">{t_i18n("Severity bands")}</h4>
          {(Object.keys(EXPOSURE_BANDS) as ExposureBand[]).map((each) => {
            const current = each === band;
            const eachColor = exposureBand({ low: 0, moderate: 25, high: 50, critical: 75 }[each], mode).color;
            return (
              <div
                key={each}
                className="flex items-start gap-3 rounded-[4px] border px-2 py-1.5"
                style={{ background: current ? tint(eachColor, 10) : "transparent", borderColor: current ? tint(eachColor, 40) : "transparent" }}
                data-current={current ? "true" : undefined}
              >
                <span className="mt-1 size-2.5 shrink-0 rounded-full" style={{ background: eachColor, boxShadow: `0 0 6px ${tint(eachColor, 70)}` }} />
                <div>
                  <div className="content-compact font-semibold">
                    {`${BAND_RANGES[each]} - ${t_i18n(EXPOSURE_BANDS[each])}`}
                    {current ? ` - ${t_i18n("current")}` : ""}
                  </div>
                  <div className="content-compact text-default-secondary">{t_i18n(BAND_DESCRIPTIONS[each])}</div>
                </div>
              </div>
            );
          })}
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button type="button" priority="secondary" onClick={onClose}>
            {t_i18n("Close")}
          </Button>
          {onInvestigate && (
            <Button
              type="button"
              onClick={() => {
                onClose();
                onInvestigate();
              }}
            >
              {t_i18n("Investigate breached validations")}
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── The attack flow ─────────────────────────────────────────────────────────

function AttackFlow({ items, mode, onGate, onAll, onBreach }: { items: WidgetOutcome[]; mode: ColorMode; onGate?: (item: WidgetOutcome) => void; onAll?: () => void; onBreach?: () => void }) {
  const { t_i18n } = useFormatter();
  const exact = useNumberFormat();
  const word = useOutcomeWords();
  const glow = `${useId().replace(/:/g, "")}-glow`;
  const svg = useVisibilityPause();
  const theme = SEMANTIC[mode];
  const dark = mode === "dark";
  const line = dark ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0.10)";
  const track = "color-mix(in srgb, var(--text-default-primary) 12%, transparent)";
  const attack = theme.error;
  const assets = "var(--text-default-secondary)";
  const { gates, attempts, pending, breached, breachParticles } = flowGates(items, mode);
  const { cy, adversaryX, assetsX, gateHalf, captionY, valueY, gaugeY, percentY } = FLOW;
  const pathTo = (endX: number) => `M ${adversaryX + 26} ${cy} L ${endX} ${cy}`;
  const hit = (label: string, action: (() => void) | undefined) => (action ? { ...pressable(label, action), style: { cursor: "pointer" } } : {});
  const caption = { fontSize: 10, letterSpacing: "0.1em", textTransform: "uppercase" as const };
  const value = { fontFamily: GEOLOGICA, fontSize: 17, fontWeight: 600 };
  const particle = (path: string, dur: number, begin: string, opacity = 1) => (
    <>
      <circle r={3.2} style={{ fill: attack }} opacity={0}>
        <animateMotion dur={`${dur}s`} begin={begin} repeatCount="indefinite" path={path} keyPoints="0;1" keyTimes="0;1" calcMode="linear" />
        <animate attributeName="opacity" values={`0;${opacity};${opacity};0`} keyTimes="0;0.08;0.9;1" dur={`${dur}s`} begin={begin} repeatCount="indefinite" />
      </circle>
      <circle r={6.5} fill={`url(#${glow})`} opacity={0}>
        <animateMotion dur={`${dur}s`} begin={begin} repeatCount="indefinite" path={path} keyPoints="0;1" keyTimes="0;1" calcMode="linear" />
        <animate attributeName="opacity" values={`0;${opacity * 0.5};${opacity * 0.5};0`} keyTimes="0;0.08;0.9;1" dur={`${dur}s`} begin={begin} repeatCount="indefinite" />
      </circle>
    </>
  );
  const impact = (x: number, color: string, dur: number, begin: string) => (
    <circle cx={x} cy={cy} r={10} fill="none" style={{ stroke: color }} strokeWidth={1.5} opacity={0}>
      <animate attributeName="opacity" values="0;0.9;0" keyTimes="0;0.1;1" dur={`${dur}s`} begin={begin} repeatCount="indefinite" />
      <animate attributeName="r" values="10;24" keyTimes="0;1" dur={`${dur}s`} begin={begin} repeatCount="indefinite" />
    </circle>
  );
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center">
      <svg ref={svg} viewBox={`0 0 ${FLOW.width} ${FLOW.height}`} preserveAspectRatio="xMidYMid meet" style={{ width: "100%", maxHeight: "100%", overflow: "visible" }} data-testid="exposure-console.flow">
        <defs>
          <radialGradient id={glow} cx="50%" cy="50%" r="50%">
            <stop offset="0%" style={{ stopColor: attack, stopOpacity: 0.9 }} />
            <stop offset="60%" style={{ stopColor: attack, stopOpacity: 0.35 }} />
            <stop offset="100%" style={{ stopColor: attack, stopOpacity: 0 }} />
          </radialGradient>
        </defs>
        <path d={`M ${adversaryX + 26} ${cy} L ${assetsX - 26} ${cy}`} fill="none" stroke={line} strokeWidth={1} strokeDasharray="3 5">
          <animate attributeName="stroke-dashoffset" values="8;0" dur="0.8s" repeatCount="indefinite" />
        </path>
        {gates.map((gate) => {
          const label = word(gate.item.label);
          const top = cy - gateHalf;
          const height = gateHalf * 2;
          const gaugeX = gate.x - gate.gaugeWidth / 2;
          const percent = Math.round(gate.rate * 100);
          const verb = word(gate.item.successLabel ?? "stopped");
          return (
            <g key={gate.item.key ?? gate.item.label} data-testid="exposure-console.gate" data-color={gate.color} data-rate-color={gate.rateColor}>
              <rect x={gate.x - 1.5} y={top} width={3} height={height} rx={1.5} style={{ fill: track }} />
              <rect x={gate.x - 3.5} y={top + height - gate.fill} width={7} height={gate.fill} rx={3.5} style={{ fill: alpha(gate.color, 0.3) }}>
                <animate attributeName="height" values={`0;${gate.fill}`} dur="1.1s" fill="freeze" calcMode="spline" keySplines="0.22 1 0.36 1" />
                <animate attributeName="y" values={`${top + height};${top + height - gate.fill}`} dur="1.1s" fill="freeze" calcMode="spline" keySplines="0.22 1 0.36 1" />
              </rect>
              <rect x={gate.x - 1.5} y={top + height - gate.fill} width={3} height={gate.fill} rx={1.5} style={{ fill: gate.color }}>
                <animate attributeName="height" values={`0;${gate.fill}`} dur="1.1s" fill="freeze" calcMode="spline" keySplines="0.22 1 0.36 1" />
                <animate attributeName="y" values={`${top + height};${top + height - gate.fill}`} dur="1.1s" fill="freeze" calcMode="spline" keySplines="0.22 1 0.36 1" />
              </rect>
              <circle cx={gate.x} cy={cy} r={5} fill={dark ? "#0c1526" : "#f4f8fc"} style={{ stroke: gate.color }} strokeWidth={1.5} />
              <circle cx={gate.x} cy={cy} r={2} style={{ fill: gate.color }}>
                <animate attributeName="r" values="2;3;2" dur={`${2.4 + (gate.index % 3) * 0.4}s`} repeatCount="indefinite" />
              </circle>
              <text x={gate.x} y={captionY} textAnchor="middle" fill="var(--text-default-secondary)" style={caption}>
                <title>{label}</title>
                {truncateLabel(label, 9.5, gate.labelWidth)}
              </text>
              <text x={gate.x} y={valueY} textAnchor="middle" style={{ ...value, fill: gate.color }}>
                <title>{exact.exactCount(gate.item.success)}</title>
                {compact(gate.item.success)}
              </text>
              <g>
                <title>{`${percent}% ${verb}`}</title>
                <rect x={gaugeX} y={gaugeY} width={gate.gaugeWidth} height={5} rx={2.5} style={{ fill: track }} />
                <rect x={gaugeX} y={gaugeY} width={gate.gaugeFill} height={5} rx={2.5} style={{ fill: gate.rateColor }}>
                  <animate attributeName="width" values={`0;${gate.gaugeFill}`} dur="1.1s" fill="freeze" calcMode="spline" keySplines="0.22 1 0.36 1" />
                </rect>
                <text x={gate.x} y={percentY} textAnchor="middle" style={{ fill: gate.rateColor, fontSize: 10, fontWeight: 600 }}>
                  {`${percent}%`}
                </text>
              </g>
              {Array.from({ length: gate.stopped }, (_, k) => (
                <g key={k}>{particle(pathTo(gate.x - 6), 2.2 + gate.index * 0.4 + k * 0.6, `${gate.index * 0.7 + k * 1.1}s`)}</g>
              ))}
              {gate.stopped > 0 && impact(gate.x, gate.color, 2.6 + gate.index * 0.3, `${1.1 + gate.index * 0.7}s`)}
              <rect
                x={gate.x - gate.column / 2}
                y={captionY - 12}
                width={gate.column}
                height={percentY - captionY + 24}
                fill="transparent"
                {...hit(label, onGate ? () => onGate(gate.item) : undefined)}
              />
            </g>
          );
        })}
        {Array.from({ length: breachParticles }, (_, i) => (
          <g key={`x${i}`}>{particle(pathTo(assetsX - 26), 4.4 + i * 0.8, `${1.8 + i * 1.9}s`, 0.9)}</g>
        ))}
        {breachParticles > 0 && impact(assetsX, attack, 4.4, "6.1s")}
        <g data-testid="exposure-console.adversary">
          <text x={adversaryX} y={captionY} textAnchor="middle" fill="var(--text-default-secondary)" style={caption}>
            {t_i18n("Adversary")}
          </text>
          <circle cx={adversaryX} cy={cy} r={22} style={{ fill: alpha(attack, 0.078), stroke: attack }} strokeWidth={1.5} />
          <circle cx={adversaryX} cy={cy} r={22} fill="none" style={{ stroke: attack }} strokeWidth={1} opacity={0.6}>
            <animate attributeName="r" values="22;33" dur="2s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.6;0" dur="2s" repeatCount="indefinite" />
          </circle>
          <circle cx={adversaryX} cy={cy} r={9} fill="none" style={{ stroke: attack }} strokeWidth={1.5} />
          {[
            [adversaryX - 14, cy, adversaryX - 5, cy],
            [adversaryX + 5, cy, adversaryX + 14, cy],
            [adversaryX, cy - 14, adversaryX, cy - 5],
            [adversaryX, cy + 5, adversaryX, cy + 14],
          ].map(([x1, y1, x2, y2]) => (
            <line key={`${x1}-${y1}`} x1={x1} y1={y1} x2={x2} y2={y2} style={{ stroke: attack }} strokeWidth={1.5} />
          ))}
          <circle cx={adversaryX} cy={cy} r={2} style={{ fill: attack }} />
          <text x={adversaryX} y={valueY} textAnchor="middle" style={{ ...value, fill: attack }}>
            <title>{pending > 0 ? `${exact.exactCount(attempts)} - ${t_i18n("Pending")}: ${exact.exactCount(pending)}` : exact.exactCount(attempts)}</title>
            {compact(attempts)}
          </text>
          <rect x={adversaryX - 34} y={captionY - 12} width={68} height={percentY - captionY + 24} fill="transparent" {...hit(t_i18n("Adversary"), onAll)} />
        </g>
        <g data-testid="exposure-console.breached" data-breached={breached > 0 ? "true" : "false"}>
          <text x={assetsX} y={captionY} textAnchor="middle" fill="var(--text-default-secondary)" style={caption}>
            {t_i18n("Breached")}
          </text>
          <circle
            cx={assetsX}
            cy={cy}
            r={24}
            style={{ fill: breached > 0 ? alpha(attack, 0.063) : dark ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.04)", stroke: breached > 0 ? attack : assets }}
            strokeWidth={1.3}
          />
          {[-8, -1, 6].map((dy) => (
            <g key={dy}>
              <rect x={assetsX - 10} y={cy + dy} width={20} height={5.5} rx={1.5} fill="none" style={{ stroke: breached > 0 ? attack : assets }} strokeWidth={1.2} />
              <circle cx={assetsX - 6} cy={cy + dy + 2.75} r={0.9} style={{ fill: breached > 0 ? attack : theme.secondary }}>
                <animate attributeName="opacity" values="1;0.2;1" dur={`${1.5 + (dy + 8) * 0.15}s`} repeatCount="indefinite" />
              </circle>
            </g>
          ))}
          <text x={assetsX} y={valueY} textAnchor="middle" style={{ ...value, fill: breached > 0 ? attack : assets }}>
            <title>{exact.exactCount(breached)}</title>
            {compact(breached)}
          </text>
          <rect x={assetsX - 34} y={captionY - 12} width={68} height={percentY - captionY + 24} fill="transparent" {...hit(t_i18n("Breached"), onBreach)} />
        </g>
      </svg>
    </div>
  );
}

// ── The action rail ─────────────────────────────────────────────────────────

function ActionRail({ actions, mode }: { actions: NonNullable<WidgetOutcomesData["actions"]>; mode: ColorMode }) {
  return (
    <div
      className="flex flex-col gap-2 rounded-[6px] border p-1.5"
      style={{ borderColor: mode === "dark" ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.12)", background: alpha("var(--bg-elevation-default)", 0.5) }}
      data-testid="exposure-console.actions"
    >
      {actions.map((action, i) => {
        const color = toneColor(action.color, mode) ?? SEMANTIC[mode].primary;
        const href = dataHref(action.link);
        return (
          <Tip key={action.link} content={action.caption ? `${action.label} - ${action.caption}` : action.label} side="left">
            <a
              href={href ?? undefined}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={action.label}
              className="relative flex size-[38px] items-center justify-center rounded-[4px] transition-all duration-200 [transition-timing-function:cubic-bezier(0.22,1,0.36,1)] hover:-translate-x-0.5 hover:[background:var(--rail-hover)] hover:[box-shadow:var(--rail-shadow)]"
              style={{
                color,
                background: alpha(color, 0.08),
                ["--rail-hover" as string]: alpha(color, 0.2),
                ["--rail-shadow" as string]: `inset 0 0 0 1px ${alpha(color, 0.5)}, 0 0 12px ${alpha(color, 0.35)}`,
              }}
            >
              <OutcomeIcon name={action.icon} size={20} />
              {i < actions.length - 1 && (
                <span className="absolute left-1/4 h-px w-1/2" style={{ bottom: -5, background: mode === "dark" ? "rgba(255,255,255,0.12)" : "rgba(0,0,0,0.12)" }} />
              )}
            </a>
          </Tip>
        );
      })}
    </div>
  );
}

export default function ExposureConsole({ data }: { data: WidgetOutcomesData }) {
  const mode = useColorMode();
  const { onSelect } = useChartEvents();
  const [explaining, setExplaining] = useState(false);
  const totals = totalsOf(data.items);
  const score = exposureScore(totals);
  const validations = totals.success + totals.failed;
  const breach = onSelect ? () => onSelect({ name: "breach", value: "breach" }) : undefined;
  return (
    <div className="grid h-full gap-3 overflow-hidden" style={{ gridTemplateColumns: "minmax(240px, 3fr) minmax(400px, 9fr) auto" }}>
      <Orb data={data} score={score} gaps={totals.failed} validations={validations} mode={mode} onExplain={() => setExplaining(true)} />
      <div className="flex min-h-0 min-w-0 flex-col">
        <AttackFlow
          items={data.items}
          mode={mode}
          onGate={onSelect ? (item) => onSelect({ name: item.label, value: item.key ?? item.label }) : undefined}
          onAll={onSelect ? () => onSelect({ name: "all", value: "all" }) : undefined}
          onBreach={breach}
        />
      </div>
      <div className="flex min-h-0 min-w-0 items-center">{data.actions && data.actions.length > 0 && <ActionRail actions={data.actions} mode={mode} />}</div>
      <Explanation
        data={data}
        score={score}
        gaps={totals.failed}
        validations={validations}
        mode={mode}
        open={explaining}
        onClose={() => setExplaining(false)}
        onInvestigate={breach}
      />
    </div>
  );
}
