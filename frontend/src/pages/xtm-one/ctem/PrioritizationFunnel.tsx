// SPDX-License-Identifier: Apache-2.0

/**
 * The CTEM prioritization funnel (#11546): how the raw vulnerability volume
 * narrows to what needs action, layer by layer (XSIAM 4.0 parity, proposal A).
 *
 * Two drawings of the same data, never the same picture twice:
 * - `variant="stage"`: the signature viz of the Prioritization stage page. One
 *   row per layer: the layer's name and definition, a centred bar as wide as
 *   the layer (square-root scale, `prioritization-funnel.ts`), the exact count
 *   and what the layer removed. Not configured layers are dashed, keep the
 *   width above them (they remove nothing) and lead to Settings > Organization.
 * - `variant="compact"`: the command center widget. One horizontal strip from
 *   the raw volume to what needs action, every live layer a step, and a link
 *   to the stage page, which carries the definitions.
 *
 * Every count, drop and findings figure is drawn compact (`useNumberFormat`,
 * "4.2K", Sam's compact-number rule, UX r2 B1 on #11557), its exact value in the
 * tooltip and the accessible name; the widths use the raw counts. Each live
 * layer opens the list it counts in OpenCTI.
 * The bars grow in once (a CSS width transition, off under reduced motion);
 * rows have a fixed height, so nothing moves when the data arrives.
 */
import {
  forwardRef,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FocusEvent,
  type HTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
} from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Card, CardContent } from "@/components/ui/card";
import { CompactFigure } from "@/components/ui/compact-figure";
import { EmptyState } from "@/components/ui/empty-state";
import { SampleBadge } from "@/components/ui/sample-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { useFormatter } from "@/i18n";
import { formatTimeAgoLine } from "@/lib/relative-time";
import { CTEM_OPEN_CASE_STATES, useCtemCases, useCtemCasesOverview } from "@/hooks/api/useCtemCasesApi";
import { useRecentCtemSignals, type CtemSignalList } from "@/hooks/api/useCtemSignalsApi";
import { BUTTON_ROLE_FOCUS_RING } from "@/lib/button-role";
import { cn, safeExternalUrl } from "@/lib/utils";
import type { CtemFunnel, CtemFunnelLayer } from "@/types";

import { casesListHref } from "./cases-due";
import { CASE_STATE_LABEL } from "./cases-shared";
import {
  FLOW_HEIGHT,
  FLOW_REASON,
  FLOW_TOP,
  FUNNEL_LAYER_COPY,
  flowBandPath,
  flowBranchPath,
  flowDashedPath,
  flowFan,
  flowFanPath,
  flowLayout,
  funnelIsEmpty,
  funnelLayerHref,
  funnelRows,
  funnelSettingsHref,
  funnelWidthPct,
  isCasesLayer,
  isInventoryHref,
  isOrganizationSettingsHref,
  withCasesLayer,
  type FlowStep,
  type FunnelCaseStates,
  type FunnelDisplay,
  type FunnelRow,
} from "./prioritization-funnel";
import { HiddenChartValues } from "./stage-heroes";
import { signalsHref } from "./loop/signals-route";

/** OpenCTI, opened in a new tab; nothing when its URL is unknown or unsafe. */
function OpenOpenCti({ openctiUrl }: { openctiUrl?: string | null }) {
  const { t_i18n } = useFormatter();
  const safe = openctiUrl ? safeExternalUrl(openctiUrl) : null;
  if (!safe) return null;
  return (
    <a
      href={safe}
      target="_blank"
      rel="noopener noreferrer"
      className={cn("content-compact font-medium text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)}
    >
      {t_i18n("Open OpenCTI")}
    </a>
  );
}

/** The empty state's glyph, from the design system (no direct lucide import); EmptyState's class sizes it. */
const FunnelEmptyGlyph = ({ className }: { className?: string }) => <Icon name="shield-check" size={24} className={className} />;

/** Fill of a live layer: the accent, deeper as the funnel narrows. */
const LIVE_FILL = ["bg-accent/25", "bg-accent/40", "bg-accent/60", "bg-accent/75", "bg-accent/85", "bg-accent", "bg-accent"];

/** The fill of each case state, the tokens of the Cases board (`CasesCountsCard`). */
const SEGMENT_FILL: Record<"waiting_decision" | "in_work", string> = {
  waiting_decision: "bg-feedback-error-primary",
  in_work: "bg-feedback-info-primary",
};

/** The two open tiles of the fan (#1503): what waits on a person, and what the agents work. */
const FAN_TILES = [
  { key: "waiting_decision", label: CASE_STATE_LABEL.waiting_decision, href: casesListHref({ state: "waiting_decision" }) },
  { key: "in_work", label: "In work", href: casesListHref({ state: "open" }) },
] as const;

/** Width the flow is laid out at before its box is measured (and in tests, which have no layout). */
const FLOW_FALLBACK_WIDTH = 1080;

/**
 * The flow box's width, kept current as it resizes (no import from the EE
 * dashboard charts). A callback ref, not a mount-only effect: the box mounts
 * only once the funnel has data (after the empty state, the loading or the
 * placeholder), so the observer attaches to the box itself whenever it mounts,
 * and its first width is measured at once, before the first paint (UX r1 B1
 * on #11678).
 */
function useFlowWidth() {
  const ref = useRef<HTMLDivElement | null>(null);
  const observer = useRef<ResizeObserver | null>(null);
  const [width, setWidth] = useState(0);
  const attach = useCallback((node: HTMLDivElement | null) => {
    observer.current?.disconnect();
    observer.current = null;
    ref.current = node;
    if (!node) return;
    const first = Math.round(node.getBoundingClientRect().width);
    if (first > 0) setWidth(first);
    if (typeof ResizeObserver === "undefined") return;
    const next = new ResizeObserver((entries) => {
      const measured = Math.round(entries[0]?.contentRect.width ?? 0);
      if (measured > 0) setWidth(measured);
    });
    next.observe(node);
    observer.current = next;
  }, []);
  return { ref, attach, width };
}

/**
 * One tab stop across the flow's marks (steps, call to action links, the cases
 * node and tiles): the active mark holds tabindex 0, the arrows move it, Enter
 * follows the link the mark is (rule C1 of the design round).
 */
function useFlowRoving(ref: { current: HTMLDivElement | null }) {
  const [active, setActive] = useState(0);
  const marks = useCallback(() => Array.from(ref.current?.querySelectorAll<HTMLElement>("[data-flow-mark]") ?? []), [ref]);
  useEffect(() => {
    const all = marks();
    const current = Math.min(active, Math.max(all.length - 1, 0));
    all.forEach((mark, i) => mark.setAttribute("tabindex", i === current ? "0" : "-1"));
  });
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const all = marks();
    if (all.length === 0) return;
    const index = all.indexOf(e.target as HTMLElement);
    if (index < 0) return;
    let next: number;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (index + 1) % all.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (index - 1 + all.length) % all.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = all.length - 1;
    else return;
    e.preventDefault();
    setActive(next);
    all[next].focus();
  };
  const onFocus = (e: FocusEvent<HTMLDivElement>) => {
    const index = marks().indexOf(e.target as HTMLElement);
    if (index >= 0) setActive(index);
  };
  return { onKeyDown, onFocus };
}

export interface PrioritizationFunnelProps {
  funnel: CtemFunnel;
  variant: "stage" | "compact";
  /** OpenCTI's base URL, for the layer links; none: the layers are not links. */
  openctiUrl?: string | null;
  /** When the counts were read from OpenCTI (the posture's `generated_at`), for the tooltip. */
  readAt?: string | null;
  /** A sample funnel (nothing connected): badged, no link. */
  sample?: boolean;
  /** The case counts by state (`GET /ctem-cases`): the cases node fans out into them. */
  caseStates?: FunnelCaseStates;
  /**
   * Compact only (#1621): the last step "In a case", the vulnerabilities (the funnel's unit) an open
   * case carries and the cases resolved in the window (`GET /ctem-cases/overview`); absent, no step.
   */
  inCase?: { count: number; resolved: number; days: number } | null;
}

/** Whether the bars are drawn at their width yet (grown in after the first paint). */
function useGrown(): boolean {
  const reduced = usePrefersReducedMotion();
  const [grown, setGrown] = useState(reduced);
  useEffect(() => {
    if (reduced) {
      setGrown(true);
      return;
    }
    const frame = requestAnimationFrame(() => setGrown(true));
    return () => cancelAnimationFrame(frame);
  }, [reduced]);
  return grown;
}

interface LayerLinkProps extends Omit<HTMLAttributes<HTMLElement>, "children"> {
  href: string | null;
  label: string;
  children: ReactNode;
}

/**
 * A layer: a link to the list it counts, else a focusable block. It forwards
 * its ref and every other prop to the element it renders, so the `<Tip>` around
 * it (TooltipTrigger asChild) opens on hover and on keyboard focus with the
 * exact value (UX r3 B1 on #11557).
 */
const LayerLink = forwardRef<HTMLElement, LayerLinkProps>(function LayerLink(
  { href, label, className, children, ...rest },
  ref,
) {
  const ring = cn("rounded-sm outline-none", BUTTON_ROLE_FOCUS_RING, className);
  // A link reads as one (#11954): the pointer and a light surface on hover,
  // the focus ring above for the keyboard; a block stays plain.
  const linkRing = cn(ring, "cursor-pointer transition-colors hover:bg-elevation-hover");
  const block = (
    <div
      {...rest}
      ref={ref as Ref<HTMLDivElement>}
      tabIndex={0}
      role="group"
      aria-label={label}
      className={ring}
    >
      {children}
    </div>
  );
  if (!href) return block;
  if (/^https?:\/\//.test(href)) {
    const safe = safeExternalUrl(href);
    if (!safe) return block;
    return (
      <a
        {...rest}
        ref={ref as Ref<HTMLAnchorElement>}
        href={safe}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={label}
        className={linkRing}
      >
        {children}
      </a>
    );
  }
  return (
    <Link {...rest} ref={ref as Ref<HTMLAnchorElement>} to={href} aria-label={label} className={linkRing}>
      {children}
    </Link>
  );
});

export function PrioritizationFunnel({ funnel, variant, openctiUrl, sample = false, caseStates, readAt, inCase = null }: PrioritizationFunnelProps) {
  const { t_i18n, intl } = useFormatter();
  const grown = useGrown();
  const measured = useFlowWidth();
  const { onKeyDown: onFlowKeyDown, onFocus: onFlowFocus } = useFlowRoving(measured.ref);
  const rows = funnelRows(funnel);
  const figures = useNumberFormat();
  /** Compact for display ("4.2K"); `exact` for the tooltip and the accessible name. */
  const n = (value: number) => figures.count(value);
  const exact = (value: number) => figures.exactCount(value);
  const threshold = exact(funnel.epss_threshold);

  const nameOf = (layer: CtemFunnelLayer) => t_i18n(FUNNEL_LAYER_COPY[layer.key].name);
  const definitionOf = (layer: CtemFunnelLayer) =>
    t_i18n(FUNNEL_LAYER_COPY[layer.key].definition, { threshold });
  /** The cases layer counts cases, not vulnerabilities: its exact value says so. */
  const casesUnit = (layer: CtemFunnelLayer) =>
    isCasesLayer(layer) && layer.count !== null
      ? t_i18n("{count, plural, =0 {No open case} one {# open case} other {# open cases}}", { count: layer.count })
      : null;
  const stateText = (layer: CtemFunnelLayer) => {
    if (layer.state === "not_configured") return t_i18n("Not configured");
    if (layer.reason === "assets_not_linked") return t_i18n("Not linked");
    if (layer.reason === "no_software") return t_i18n("No software inventory");
    return t_i18n("Not available yet");
  };
  /** What the business impact and policy layers read from the live configuration (#1980); null elsewhere. */
  const scopeText = (layer: CtemFunnelLayer) => {
    if (layer.key === "business_impact" && layer.primary_assets != null && layer.supporting_assets != null) {
      return t_i18n(
        "{primary, plural, one {# primary asset} other {# primary assets}} reached through {supporting, plural, one {# supporting asset} other {# supporting assets}}.",
        { primary: layer.primary_assets, supporting: layer.supporting_assets },
      );
    }
    if (layer.key === "policy" && layer.rules && layer.rules.length > 0) {
      return t_i18n("{count, plural, one {# policy rule applied} other {# policy rules applied}}.", { count: layer.rules.length });
    }
    return null;
  };
  /** The reachability split of the profile assets the Exposed layer read; null on any other layer. */
  const reachabilityText = (layer: CtemFunnelLayer) =>
    layer.reachability && layer.reachability.total > 0
      ? t_i18n(
          "{total, plural, one {# asset read} other {# assets read}}. Reachable: {reachable} (user endpoints: {endpoints}). Proven unreachable: {unreachable}. Unknown: {unknown}.",
          {
            total: layer.reachability.total,
            reachable: exact(layer.reachability.reachable),
            endpoints: exact(layer.reachability.endpoints),
            unreachable: exact(layer.reachability.unreachable),
            unknown: exact(layer.reachability.unknown),
          },
        )
      : null;
  const dropText = (row: FunnelRow, format: (value: number) => string = n) =>
    row.layer.drop !== null && row.fromKey
      ? t_i18n("{count} fewer than {layer}", { count: format(row.layer.drop), layer: t_i18n(FUNNEL_LAYER_COPY[row.fromKey].name) })
      : null;
  const hrefOf = (layer: CtemFunnelLayer) => (sample ? null : funnelLayerHref(layer, openctiUrl));
  const accessibleName = (row: FunnelRow) => {
    const layer = row.layer;
    const value = layer.count !== null ? exact(layer.count) : stateText(layer);
    return [nameOf(layer), casesUnit(layer) ?? value, dropText(row, exact)].filter(Boolean).join(", ");
  };
  /** Where a layer's link leads, in words (#11954); null when it is not a link. */
  const destinationOf = (layer: CtemFunnelLayer) => {
    const href = hrefOf(layer);
    if (!href) return null;
    if (isOrganizationSettingsHref(href)) return t_i18n("Opens Settings > Organization");
    if (isInventoryHref(href)) return t_i18n("Opens CTEM > Inventory");
    if (/^https?:\/\//.test(href)) return t_i18n("Opens in OpenCTI, Arsenal > Vulnerabilities");
    return t_i18n("Opens the Cases list");
  };
  const readText =
    readAt && !sample
      ? formatTimeAgoLine(intl, Date.parse(readAt), {
          line: (timeAgo) => t_i18n("Read {timeAgo}", { timeAgo }),
          justNow: () => t_i18n("Read just now"),
          fallback: readAt,
        })
      : null;
  const tip = (row: FunnelRow) => {
    const destination = destinationOf(row.layer);
    const external = destination !== null && /^https?:\/\//.test(hrefOf(row.layer) ?? "");
    return (
      <div className="max-w-64 space-y-1">
        <p className="font-semibold">
          {nameOf(row.layer)}: {casesUnit(row.layer) ?? (row.layer.count !== null ? exact(row.layer.count) : stateText(row.layer))}
        </p>
        <p>{definitionOf(row.layer)}</p>
        {reachabilityText(row.layer) && <p>{reachabilityText(row.layer)}</p>}
        {scopeText(row.layer) && <p data-testid={`ctem-funnel-scope-${row.layer.key}`}>{scopeText(row.layer)}</p>}
        {dropText(row, exact) && <p>{dropText(row, exact)}</p>}
        {row.layer.state === "live" && readText && <p>{readText}</p>}
        {destination && (
          <p className="flex items-center gap-1 font-medium" data-testid={`ctem-funnel-destination-${row.layer.key}`}>
            {external && <Icon name="external-link" size={16} className="shrink-0" />}
            {destination}
          </p>
        )}
      </div>
    );
  };

  // OpenCTI answered 0: an empty state, never a full bar of 0 (UX r1 B1 on #11557).
  if (!sample && funnelIsEmpty(funnel)) {
    if (variant === "compact") {
      return (
        <div className="flex h-full items-center gap-3" data-testid="ctem-funnel-empty">
          <p className="content-compact text-default-secondary">{t_i18n("No vulnerabilities in your OpenCTI yet")}</p>
          <OpenOpenCti openctiUrl={openctiUrl} />
        </div>
      );
    }
    return (
      <div className="space-y-3" data-testid="ctem-funnel-empty">
        <h2 className="content-base font-semibold">{t_i18n("Prioritization funnel")}</h2>
        <EmptyState
          variant="inset"
          icon={FunnelEmptyGlyph}
          title="No vulnerabilities in your OpenCTI yet"
          description="The funnel shows how many need action once OpenCTI holds vulnerabilities."
        >
          <OpenOpenCti openctiUrl={openctiUrl} />
        </EmptyState>
      </div>
    );
  }

  if (variant === "compact") {
    const live = rows.filter((row) => row.layer.state === "live" && row.layer.count !== null);
    const unconfigured = rows.filter((row) => row.layer.state === "not_configured").length;
    return (
      <div className="flex h-full flex-col gap-3" data-testid="ctem-funnel-compact">
        <ol className="flex min-h-0 flex-1 items-stretch gap-1.5" aria-label={t_i18n("Prioritization funnel")}>
          {live.map((row, index) => (
            <li key={row.layer.key} className="flex min-w-0 flex-1 flex-col justify-end gap-1">
              <Tip content={tip(row)}>
                <LayerLink href={hrefOf(row.layer)} label={accessibleName(row)} className="flex min-w-0 flex-col gap-1">
                  <span className="content-caption truncate text-default-secondary">{nameOf(row.layer)}</span>
                  <span className="content-base font-semibold tabular-nums" data-testid={`ctem-funnel-count-${row.layer.key}`}>
                    {n(row.layer.count as number)}
                  </span>
                  {/* A sample dims its bars only, never its text (UX r2 A1 on #11557). */}
                  <span className={cn("relative block h-2 overflow-hidden rounded-full bg-elevation-hover", sample && "opacity-60")}>
                    <span
                      className={cn(
                        "absolute inset-y-0 left-0 rounded-full transition-[width] duration-700 ease-out motion-reduce:transition-none",
                        LIVE_FILL[Math.min(index + 2, LIVE_FILL.length - 1)],
                      )}
                      style={{ width: grown ? `${row.widthPct}%` : "0%" }}
                    />
                  </span>
                </LayerLink>
              </Tip>
            </li>
          ))}
          {inCase && !sample && (
            <li className="flex min-w-0 flex-1 flex-col justify-end gap-1" data-testid="ctem-funnel-step-in-case">
              <Tip
                content={
                  <div className="max-w-64 space-y-1">
                    <p className="font-semibold">{t_i18n("In a case")}: {exact(inCase.count)}</p>
                    <p>{t_i18n("Vulnerabilities an open case carries, among the ones you can see.")}</p>
                    <p>{t_i18n("{count, plural, one {# case resolved in the last {days} days} other {# cases resolved in the last {days} days}}", { count: inCase.resolved, days: inCase.days })}</p>
                    <p className="font-medium">{t_i18n("Opens the Cases list")}</p>
                  </div>
                }
              >
                <LayerLink
                  href="/ctem/cases"
                  label={t_i18n("In a case, {count}, {resolved} resolved in {days} d", { count: exact(inCase.count), resolved: inCase.resolved, days: inCase.days })}
                  className="flex min-w-0 flex-col gap-1"
                >
                  <span className="content-caption truncate text-default-secondary">
                    {t_i18n("In a case")}
                    <span className="tabular-nums"> · {t_i18n("{resolved} resolved in {days} d", { resolved: inCase.resolved, days: inCase.days })}</span>
                  </span>
                  <span className="content-base font-semibold tabular-nums" data-testid="ctem-funnel-count-in-case">
                    {n(inCase.count)}
                  </span>
                  <span className="relative block h-2 overflow-hidden rounded-full bg-elevation-hover">
                    <span
                      className={cn(
                        "absolute inset-y-0 left-0 rounded-full transition-[width] duration-700 ease-out motion-reduce:transition-none",
                        LIVE_FILL[LIVE_FILL.length - 1],
                      )}
                      style={{ width: grown ? `${funnelWidthPct(inCase.count, funnel.layers[0]?.count ?? 0)}%` : "0%" }}
                    />
                  </span>
                </LayerLink>
              </Tip>
            </li>
          )}
        </ol>
        {unconfigured > 0 && (
          <p className="content-caption text-default-secondary" data-testid="ctem-funnel-compact-unconfigured">
            {t_i18n("{count, plural, one {# layer not configured} other {# layers not configured}}", { count: unconfigured })}
          </p>
        )}
      </div>
    );
  }

  // The stage drawing: the exclusion flow (dev-docs/ctem-stage-viz.md 3.3).
  const layout = flowLayout(funnel, measured.width || FLOW_FALLBACK_WIDTH);
  const casesRow = rows.find((row) => isCasesLayer(row.layer));
  const casesLive = casesRow?.layer.state === "live" && casesRow.layer.count !== null && !!caseStates;
  const fan = casesLive ? flowFan(layout.node, caseStates.waiting_decision, caseStates.in_work) : [];
  const rowOf = (key: string) => rows.find((row) => row.layer.key === key) as FunnelRow;
  const reasonOf = (step: FlowStep) => (FLOW_REASON[step.layer.key] ? t_i18n(FLOW_REASON[step.layer.key] as string) : "");
  const tableRows = rows.map((row) => {
    const step = layout.steps.find((s) => s.layer.key === row.layer.key);
    return [
      nameOf(row.layer),
      casesUnit(row.layer) ?? (row.layer.count !== null ? exact(row.layer.count) : stateText(row.layer)),
      step?.removed != null ? exact(step.removed) : "",
      step && step.removed != null ? reasonOf(step) : "",
    ];
  });
  const bandPath = flowBandPath(layout);
  return (
    <div className="space-y-3" data-testid="ctem-funnel">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="content-base font-semibold">{t_i18n("Prioritization funnel")}</h2>
        <div className="flex items-center gap-2">
          {sample && <SampleBadge />}
          <p
            className="content-compact text-default-secondary"
            data-testid="ctem-funnel-needs-action"
            title={n(funnel.needs_action) !== exact(funnel.needs_action) ? exact(funnel.needs_action) : undefined}
          >
            {t_i18n("{count, plural, one {{shown} vulnerability needs action} other {{shown} vulnerabilities need action}}", {
              count: funnel.needs_action,
              shown: n(funnel.needs_action),
            })}
          </p>
        </div>
      </div>
      <div
        ref={measured.attach}
        className="relative w-full overflow-hidden"
        style={{ height: FLOW_HEIGHT }}
        onKeyDown={onFlowKeyDown}
        onFocus={onFlowFocus}
        data-testid="ctem-funnel-flow"
      >
        <svg
          className="absolute inset-0 h-full w-full"
          viewBox={`0 0 ${layout.width} ${FLOW_HEIGHT}`}
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <g
            className="transition-transform duration-700 ease-out motion-reduce:transition-none"
            style={{ transform: grown ? "scaleX(1)" : "scaleX(0)", transformOrigin: "0 0" }}
          >
            {bandPath && <path d={bandPath} className={cn("fill-accent", sample && "opacity-60")} data-testid="ctem-flow-band" />}
            {layout.steps.map((step) =>
              step.labelY === null ? null : step.branch > 0 ? (
                <path
                  key={step.layer.key}
                  d={flowBranchPath(layout, step)}
                  className="fill-feedback-neutral-secondary"
                  fillOpacity={0.55}
                  data-testid={`ctem-flow-branch-${step.layer.key}`}
                  data-branch={step.branch.toFixed(2)}
                />
              ) : (
                <path
                  key={step.layer.key}
                  d={flowDashedPath(layout, step)}
                  className="fill-none stroke-current text-default-secondary"
                  strokeOpacity={0.6}
                  strokeWidth={1.5}
                  strokeDasharray="4 4"
                  data-testid={`ctem-flow-branch-${step.layer.key}`}
                  data-branch="0"
                />
              ),
            )}
            {casesRow && (
              <rect x={layout.flowWidth} y={FLOW_TOP} width={8} height={layout.steps[layout.steps.length - 1]?.height ?? 0} className="fill-accent" />
            )}
            {fan.map(([s0, s1], i) => (
              <path
                key={i}
                d={flowFanPath(layout, s0, s1, i)}
                className={i === 0 ? "fill-feedback-alert-primary" : "fill-feedback-info-primary"}
                fillOpacity={0.55}
              />
            ))}
          </g>
        </svg>

        <ol aria-label={t_i18n("Prioritization funnel")}>
          {layout.steps.map((step) => {
            const row = rowOf(step.layer.key);
            const layer = step.layer;
            const live = layer.state === "live" && layer.count !== null;
            return (
              <li
                key={layer.key}
                className="absolute top-0"
                style={{ left: step.x, width: Math.max(layout.pitch - 8, 0) }}
                data-testid={`ctem-funnel-layer-${layer.key}`}
                data-state={layer.state}
              >
                <Tip content={tip(row)}>
                  <LayerLink href={hrefOf(layer)} label={accessibleName(row)} className="block" data-flow-mark="" tabIndex={-1}>
                    <span
                      className={cn(
                        "block tabular-nums",
                        live
                          ? "whitespace-nowrap content-base font-semibold text-default-primary"
                          : "truncate content-compact text-default-secondary",
                      )}
                      data-testid={`ctem-funnel-count-${layer.key}`}
                    >
                      {live ? n(layer.count as number) : stateText(layer)}
                    </span>
                  </LayerLink>
                </Tip>
                <div className="h-10">
                  <TruncatedText text={nameOf(layer)} lines={2} className="content-caption text-default-secondary" />
                </div>
              </li>
            );
          })}
        </ol>

        {layout.steps.map((step) => {
          if (step.labelY === null) return null;
          const layer = step.layer;
          const cta = layer.state === "not_configured" ? FUNNEL_LAYER_COPY[layer.key].cta : undefined;
          const biggest = layout.biggest === layer.key;
          return (
            <div
              key={layer.key}
              className="absolute flex h-5 min-w-0 items-center gap-1.5 overflow-hidden content-caption"
              style={{ left: step.labelX, top: step.labelY - 10, width: step.labelWidth }}
              data-testid={`ctem-flow-label-${layer.key}`}
            >
              {step.removed != null ? (
                <>
                  <span className={cn("tabular-nums text-default-primary", biggest ? "font-bold" : "font-semibold")}>
                    <CompactFigure exact={exact(step.removed)}>{n(step.removed)}</CompactFigure>
                  </span>
                  <span className={cn("min-w-0 flex-1 text-default-secondary", biggest && "font-semibold text-default-primary")}>
                    <TruncatedText text={reasonOf(step)} />
                  </span>
                </>
              ) : (
                <>
                  <span className="shrink-0 text-default-secondary">{stateText(layer)}</span>
                  {cta && !sample && (
                    <Link
                      to={hrefOf(layer) ?? funnelSettingsHref(layer.key)}
                      className={cn("min-w-0 truncate font-medium text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)}
                      data-flow-mark=""
                      tabIndex={-1}
                    >
                      {t_i18n(cta)}
                    </Link>
                  )}
                </>
              )}
            </div>
          );
        })}

        {casesRow && (
          <div
            className={cn(
              "absolute flex flex-col justify-center rounded-sm border-2 px-3",
              casesLive ? "border-accent bg-elevation-default" : "border-dashed border-elevation-subtle",
            )}
            style={{
              left: layout.node.x,
              top: layout.node.y,
              width: casesLive ? layout.node.width : Math.max(layout.width - layout.node.x - 2, 0),
              height: layout.node.height,
            }}
            data-testid="ctem-funnel-layer-open_issues"
            data-state={casesRow.layer.state}
          >
            <Tip content={tip(casesRow)}>
              <LayerLink href={hrefOf(casesRow.layer)} label={accessibleName(casesRow)} className="block" data-flow-mark="" tabIndex={-1}>
                <span
                  className={cn("block tabular-nums", casesLive ? "content-base font-semibold" : "content-compact text-default-secondary")}
                  data-testid="ctem-funnel-count-open_issues"
                >
                  {casesRow.layer.count !== null ? n(casesRow.layer.count) : stateText(casesRow.layer)}
                </span>
              </LayerLink>
            </Tip>
            <TruncatedText text={t_i18n("Open cases")} lines={2} className="content-caption text-default-secondary" />
          </div>
        )}

        {casesLive &&
          FAN_TILES.map(({ key: state, label: english, href }, i) => {
            const count = caseStates[state];
            const label = t_i18n(english);
            return (
              <Link
                key={state}
                to={href}
                aria-label={t_i18n("{state}: {count, plural, one {# case} other {# cases}}", { count, state: label })}
                className={cn(
                  "absolute flex flex-col justify-center overflow-hidden rounded-sm border border-elevation-subtle-soft bg-elevation-default pl-3 pr-2",
                  BUTTON_ROLE_FOCUS_RING,
                )}
                style={{ left: layout.tiles.x, top: layout.tiles.ys[i], width: layout.tiles.width, height: layout.tiles.height }}
                data-testid={`ctem-flow-tile-${state}`}
                data-flow-mark=""
                tabIndex={-1}
              >
                <span className={cn("absolute inset-y-0 left-0 w-0.5", SEGMENT_FILL[state])} aria-hidden="true" />
                <span className="content-base font-semibold tabular-nums">{n(count)}</span>
                <span className="truncate content-caption text-default-secondary">{label}</span>
              </Link>
            );
          })}

        {casesLive && (
          // Mitigated, watching on its own line above the closed line, and no
          // clamp: every count shows at 1080, where the column is 118 px (UX
          // round 2 on #897, B4; #865 prod retest D1 for the watching state).
          // Placed by the layout below the top branch label, the labels beside
          // it ending before the column (UX round 3 on #897, B4).
          <div
            className="absolute flex flex-col gap-1 content-caption text-default-secondary"
            style={{ left: layout.tiles.x, top: layout.outcomesTop, width: layout.tiles.width }}
            data-testid="ctem-flow-cases-outcomes"
          >
            {(caseStates.mitigated_watching ?? 0) > 0 && (
              <p>
                <Link
                  to={casesListHref({ state: "mitigated_watching" })}
                  className={cn("text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)}
                  data-flow-mark=""
                  tabIndex={-1}
                  data-testid="ctem-flow-cases-mitigated"
                >
                  {/* Inline, on the text baseline (UX round 1 on #897, B2). */}
                  <Icon name="eye" size={16} aria-hidden className="mr-1 inline-block align-text-bottom" />
                  {t_i18n("{count, plural, one {# mitigated, watching} other {# mitigated, watching}}", { count: caseStates.mitigated_watching ?? 0 })}
                </Link>
              </p>
            )}
            <p data-testid="ctem-flow-cases-closed">
              <Link to={casesListHref({ state: "resolved" })} className={cn("text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)} data-flow-mark="" tabIndex={-1}>
                {t_i18n("{count, plural, one {# resolved} other {# resolved}}", { count: caseStates.resolved })}
              </Link>
              {", "}
              <Link to={casesListHref({ state: "accepted_risk" })} className={cn("text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)} data-flow-mark="" tabIndex={-1}>
                {t_i18n("{count, plural, one {# accepted risk} other {# accepted risk}}", { count: caseStates.accepted_risk })}
              </Link>
            </p>
          </div>
        )}
      </div>
      {casesLive && (
        <p className="content-compact text-default-secondary" data-testid="ctem-flow-outcome">
          {t_i18n("{count, plural, one {# issue} other {# issues}} grouped into {cases, plural, one {# open case} other {# open cases}}", {
            count: funnel.needs_action,
            cases: casesRow?.layer.count ?? 0,
          })}
        </p>
      )}
      {(() => {
        const known = funnel.layers[0];
        return known?.asset_findings != null ? (
          <p className="content-caption text-default-secondary tabular-nums" title={n(known.asset_findings) !== exact(known.asset_findings) ? exact(known.asset_findings) : undefined}>
            {t_i18n("{count, plural, one {{shown} CVE finding on your assets (OpenAEV)} other {{shown} CVE findings on your assets (OpenAEV)}}", {
              count: known.asset_findings,
              shown: n(known.asset_findings),
            })}
          </p>
        ) : null;
      })()}
      <HiddenChartValues
        caption={t_i18n("Prioritization funnel")}
        head={[t_i18n("Layer"), t_i18n("Count"), t_i18n("Removed"), t_i18n("Reason")]}
        rows={tableRows}
      />
    </div>
  );
}

/**
 * The stage drawing of a live funnel, its last layer filled with the open
 * cases the caller sees (`GET /ctem-cases`, #11545 S5b). Until the Cases API
 * answers, or when it fails, the layer keeps the backend's state without a
 * count: never an invented 0.
 */
export function PrioritizationFunnelWithCases({
  funnel,
  openctiUrl,
  readAt,
}: {
  funnel: CtemFunnel;
  openctiUrl?: string | null;
  readAt?: string | null;
}) {
  // A section of the stage page: a failed read keeps the layer "Not available
  // yet" with no fan instead of handing the page to the error boundary (UX r1
  // B2 on #11678); a previous answer kept by the query is not shown as current.
  const { data: read, isError } = useCtemCases({ throwOnError: false });
  const data = isError ? undefined : read;
  const cases = funnelCasesOf(data);
  const caseStates: FunnelCaseStates | undefined = data
    ? {
        waiting_decision: data.counts.waiting_decision ?? 0,
        in_work: CTEM_OPEN_CASE_STATES.reduce((sum, s) => sum + (s === "waiting_decision" ? 0 : (data.counts[s] ?? 0)), 0),
        mitigated_watching: data.counts.mitigated_watching ?? 0,
        resolved: data.counts.resolved ?? 0,
        accepted_risk: data.counts.accepted_risk ?? 0,
      }
    : undefined;
  return (
    <div className="space-y-3">
      <PrioritizationFunnel variant="stage" funnel={withCasesLayer(funnel, cases)} openctiUrl={openctiUrl} caseStates={caseStates} readAt={readAt} />
      <SignalGateCounts />
    </div>
  );
}

/**
 * The two gates of the loop as numbers (#874, "10 flagged, 3 real"): signals
 * in, confirmed by reconcile, in scope, cases. Read from the counts by state of
 * `GET /ctem-signals` (the recent read, refreshed by `ctem_signal_update`), so it
 * moves live. A signal still waiting for reconcile counts in "in" only.
 */
export function signalGateCounts(counts: CtemSignalList["counts"]) {
  const c = (state: keyof CtemSignalList["counts"]) => counts[state] ?? 0;
  const cases = c("case_opened");
  const inScope = cases + c("confirmed");
  const confirmed = inScope + c("watchlist");
  const total = confirmed + c("new") + c("to_validate") + c("discarded");
  return { total, confirmed, inScope, cases, watchlist: c("watchlist") };
}

function SignalGateCounts() {
  const { t_i18n } = useFormatter();
  const { data, isError } = useRecentCtemSignals(1);
  // Never an invented 0: no line until the signals API answers, none on a refused read.
  if (!data || isError) return null;
  const gate = signalGateCounts(data.counts);
  if (gate.total === 0) return null;
  return (
    <p className="content-compact text-default-secondary tabular-nums" data-testid="ctem-funnel-gate-counts">
      {/* UX r1 B1 on #875: the card already counts open cases; this line names its
          scope (signals) so its "opened a case" never reads as a second case total. */}
      {t_i18n("Signals: {total} in, {confirmed} confirmed, {inScope} in scope, {cases} opened a case, {watchlist} on the watchlist", gate)}
      {/* #874 S2: the watchlist is a filter of the signals list (`/ctem/signals?state=watchlist`). */}
      {gate.watchlist > 0 && (
        <>
          {" "}
          <Link
            to={signalsHref({ state: "watchlist" })}
            className={cn("rounded-sm text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)}
            data-testid="ctem-funnel-gate-watchlist"
          >
            {t_i18n("Show the watchlist")}
          </Link>
        </>
      )}
    </p>
  );
}

/** The funnel slot of a tenant with OpenCTI connected and no funnel in its
 *  posture (#11662): never the sample, whose counts would read as the tenant's.
 *  `pending`: a posture cached before the funnel shipped, rebuilt by the server
 *  at once, the poll swaps the live funnel in; `unavailable`: OpenCTI gave no
 *  vulnerability count. */
export function PrioritizationFunnelPlaceholder({
  display,
  variant,
  openctiUrl,
}: {
  display: Extract<FunnelDisplay, "pending" | "unavailable">;
  variant: "stage" | "compact";
  openctiUrl?: string | null;
}) {
  const { t_i18n } = useFormatter();
  if (display === "unavailable") {
    if (variant === "compact") {
      return (
        <div className="flex h-full items-center gap-3" data-testid="ctem-funnel-unavailable">
          <p className="content-compact text-default-secondary">{t_i18n("OpenCTI sent no vulnerability count")}</p>
          <OpenOpenCti openctiUrl={openctiUrl} />
        </div>
      );
    }
    return (
      <div className="space-y-3" data-testid="ctem-funnel-unavailable">
        <h2 className="content-base font-semibold">{t_i18n("Prioritization funnel")}</h2>
        <EmptyState
          variant="inset"
          icon={FunnelEmptyGlyph}
          title="OpenCTI sent no vulnerability count"
          description="The funnel is drawn at the next sync that reads your vulnerabilities from OpenCTI."
        >
          <OpenOpenCti openctiUrl={openctiUrl} />
        </EmptyState>
      </div>
    );
  }
  if (variant === "compact") {
    // The live compact block's shape, line for line (UX r1 B1 on #11676): three
    // columns of a caption line, a count line and the bar, then the caption line
    // the live strip uses for its unconfigured layers, here the status. Each line
    // keeps its type size's line height, so the card does not grow when the
    // funnel lands.
    return (
      <div className="flex h-full flex-col gap-3" data-testid="ctem-funnel-pending">
        <div className="flex min-h-0 flex-1 items-stretch gap-1.5" aria-hidden="true">
          {[0, 1, 2].map((column) => (
            <div key={column} className="flex min-w-0 flex-1 flex-col justify-end gap-1" data-testid="ctem-funnel-pending-column">
              <span className="content-caption relative block">
                &nbsp;
                <Skeleton className="absolute inset-y-0.5 left-0 w-2/3" />
              </span>
              <span className="content-base relative block font-semibold">
                &nbsp;
                <Skeleton className="absolute inset-y-1 left-0 w-1/3" />
              </span>
              <Skeleton className="h-2 w-full rounded-full" />
            </div>
          ))}
        </div>
        <p role="status" className="content-caption text-default-secondary">
          {t_i18n("Reading your vulnerabilities from OpenCTI")}
        </p>
      </div>
    );
  }
  const status = (
    <p role="status" className="content-compact text-default-secondary">
      {t_i18n("Reading your vulnerabilities from OpenCTI")}
    </p>
  );
  return (
    <div className="space-y-3" data-testid="ctem-funnel-pending">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="content-base font-semibold">{t_i18n("Prioritization funnel")}</h2>
        {status}
      </div>
      {/* The rows keep the live funnel's height, so nothing moves when it lands. */}
      <div className="space-y-1.5" aria-hidden="true">
        {Object.keys(FUNNEL_LAYER_COPY).map((key, index) => (
          <div key={key} className="grid h-14 grid-cols-[minmax(8rem,14rem)_1fr_minmax(7rem,10rem)] items-center gap-3">
            <Skeleton className="h-4 w-32" />
            <div className="mx-auto" style={{ width: `${Math.max(100 - index * 12, 28)}%` }}>
              <Skeleton className="h-10 w-full" />
            </div>
            <span />
          </div>
        ))}
      </div>
    </div>
  );
}

/** The open and total case counts the cases layer reads, from a `GET /ctem-cases` answer; undefined before it. */
function funnelCasesOf(data: { counts: Partial<Record<string, number>> } | undefined) {
  return data
    ? {
        open: CTEM_OPEN_CASE_STATES.reduce((sum, s) => sum + (data.counts[s] ?? 0), 0),
        total: Object.values(data.counts).reduce<number>((sum, value) => sum + (value ?? 0), 0),
      }
    : undefined;
}

/** The command center widget: the compact funnel in a card, linking to the stage page.
 *  `placeholder`: OpenCTI connected and no funnel yet (#11662), drawn instead of the funnel. */
export function PrioritizationFunnelCard({
  funnel,
  sample,
  openctiUrl,
  placeholder,
  readAt,
}: {
  funnel: CtemFunnel;
  sample: boolean;
  openctiUrl?: string | null;
  placeholder?: Extract<FunnelDisplay, "pending" | "unavailable">;
  readAt?: string | null;
}) {
  const { t_i18n } = useFormatter();
  // The funnel ends in action (#1621): the vulnerabilities an open case carries, same unit as the steps.
  const { data: overview } = useCtemCasesOverview();
  const inCase = overview ? { count: overview.funnel.in_case, resolved: overview.funnel.resolved_recent, days: overview.funnel.resolved_window_days } : null;
  // The cases layer reads the Cases API here too (#1980): the card never says "not available" over open cases.
  const { data: read, isError } = useCtemCases({ throwOnError: false });
  const cases = sample || isError ? undefined : funnelCasesOf(read);
  return (
    <Card className="flex h-full flex-col overflow-hidden" data-testid="ctem-funnel-card">
      <CardContent className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex items-center justify-between gap-2">
          <h3 className="content-base font-semibold">{t_i18n("Prioritization funnel")}</h3>
          <div className="flex items-center gap-2">
            {sample && <SampleBadge />}
            <Link
              to="/ctem/prioritization"
              className={cn("content-compact font-medium text-accent hover:underline", BUTTON_ROLE_FOCUS_RING)}
            >
              {t_i18n("Open Prioritization")}
            </Link>
          </div>
        </div>
        <div className="min-h-0 flex-1">
          {placeholder ? (
            <PrioritizationFunnelPlaceholder display={placeholder} variant="compact" openctiUrl={openctiUrl} />
          ) : (
            <PrioritizationFunnel funnel={withCasesLayer(funnel, cases)} variant="compact" openctiUrl={openctiUrl} sample={sample} readAt={readAt} inCase={inCase} />
          )}
        </div>
      </CardContent>
    </Card>
  );
}
