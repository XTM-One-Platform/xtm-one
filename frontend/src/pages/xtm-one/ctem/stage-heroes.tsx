// SPDX-License-Identifier: Apache-2.0
/**
 * The hero viz of the CTEM stage pages (dev-docs/ctem-stage-viz.md sections 2
 * and 3, slice S1): the Scoping attack surface ring, the Discovery tested vs
 * untested area and the Validation outcome matrix, with the shared pieces they
 * use (not-configured frame, visually hidden data table, framework selector).
 *
 * Every mark is drawn from a value the posture API returns; a missing source
 * draws the dashed not-configured state with one way to connect it. No kill
 * chain, phase or technique is a constant here: the chains come from the data.
 */
import { useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { Chip } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { CompactFigure } from "@/components/ui/compact-figure";
import { InfoTip } from "@/components/ui/info-tip";
import { useElementWidth } from "@/components/custom-dashboard/charts/common";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn, safeExternalUrl } from "@/lib/utils";
import type { CtemAttackDomain, CtemMatrixItem, CtemMatrixOutcome, CtemOutcomeMatrix } from "@/types";
import { frameworkLabel } from "./ctem-framework-labels";

const SUCCESS = "var(--color-feedback-success-primary)";
const WARNING = "var(--color-feedback-warning-primary)";
const DANGER = "var(--color-feedback-error-primary)";

/** One pattern per outcome, so colour is never the only key (section 2). */
function HeroPatterns({ id }: { id: string }) {
  return (
    <defs>
      <pattern id={`${id}-diag`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="6" height="6" fill={WARNING} opacity="0.35" />
        <line x1="0" y1="0" x2="0" y2="6" stroke={WARNING} strokeWidth="3" />
      </pattern>
      <pattern id={`${id}-cross`} width="6" height="6" patternUnits="userSpaceOnUse">
        <rect width="6" height="6" fill={DANGER} opacity="0.25" />
        <path d="M0 0L6 6M6 0L0 6" stroke={DANGER} strokeWidth="1.2" />
      </pattern>
      <pattern id={`${id}-hatch`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="6" stroke={DANGER} strokeWidth="1.5" />
      </pattern>
    </defs>
  );
}

/** The dashed not-configured state: one line of reason and one outline action. */
export function NotConfiguredFrame({
  reason,
  action,
  onAction,
  href,
  testId,
}: {
  reason: string;
  /** No action: the reason stands alone (a reader who cannot fill the setting). */
  action?: string;
  onAction?: () => void;
  href?: string;
  testId?: string;
}) {
  return (
    <div
      className="flex flex-wrap items-center gap-3 rounded-sm border border-dashed border-elevation-subtle-soft px-4 py-3"
      data-testid={testId}
    >
      <p className="content-compact text-default-secondary flex-1 min-w-0">{reason}</p>
      {!action ? null : href ? (
        <Button size="sm" variant="outline" asChild>
          <a href={href}>{action}</a>
        </Button>
      ) : (
        <Button size="sm" variant="outline" onClick={onAction}>
          {action}
        </Button>
      )}
    </div>
  );
}

/**
 * The values of a chart as a table only screen readers see (section 2). The
 * sr-only box is a div around the table: a table lays out to its content width
 * whatever its own width, so an sr-only table can push the page sideways.
 */
export function HiddenChartValues({ caption, head, rows }: { caption: string; head: string[]; rows: (string | number)[][] }) {
  return (
    <div className="sr-only" data-testid="ctem-hidden-data-table">
      <table>
        <caption>{caption}</caption>
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h} scope="col">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((c, j) => (
                <td key={j}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Clamp a stored mark index to the marks drawn now (the series can shrink while mounted). */
export function clampMarkIndex(index: number, count: number): number {
  return count > 0 ? Math.min(Math.max(index, 0), count - 1) : 0;
}

/** Roving tabindex over a chart's marks: one tab stop, arrows move, Enter follows. */
export function useRovingMarks(count: number) {
  const [stored, setActive] = useState(0);
  const active = clampMarkIndex(stored, count);
  const onKeyDown = (e: KeyboardEvent, onEnter?: (i: number) => void) => {
    if (count === 0) return;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => (clampMarkIndex(a, count) + 1) % count);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => (clampMarkIndex(a, count) - 1 + count) % count);
    } else if (e.key === "Enter" && onEnter) {
      e.preventDefault();
      onEnter(active);
    }
  };
  return { active, setActive, onKeyDown };
}

export function HeroCard({
  title,
  chips,
  children,
  testId,
  className,
  hint,
}: {
  title: string;
  chips?: ReactNode;
  children: ReactNode;
  testId: string;
  className?: string;
  /** Explanation of the card, shown in an InfoTip beside the title (Design standards s.7), never as a line under it. */
  hint?: ReactNode;
}) {
  return (
    // The shared Card (design system Paper, layer 1), the surface every other card of the
    // CTEM pages uses, so a page never shows two card surfaces side by side (#1624, UX B1).
    <Card className={cn("p-5 space-y-4", className)} data-testid={testId}>
      <header className="flex flex-wrap items-center gap-2">
        <h2 className="content-base font-semibold">{title}</h2>
        {hint && <InfoTip content={hint} label={title} />}
        <div className="ml-auto flex flex-wrap items-center gap-2">{chips}</div>
      </header>
      {children}
    </Card>
  );
}

// ── Scoping: attack surface ring (3.1) ───────────────────────────────────────

const RING_R = 70;
/** Room around the ring for the domain labels, so none is drawn past the SVG (UX round 8, B1). */
const RING_LABEL_W = 112;
const RING_LABEL_H = 30;
const RING_W = 2 * (RING_R + 26 + RING_LABEL_W);
const RING_H = 2 * (RING_R + 26 + RING_LABEL_H);

/** A segment of the ring, from angle a0 to a1 (radians, 0 at 12 o'clock). */
function arcPath(cx: number, cy: number, r: number, a0: number, a1: number) {
  const p = (a: number) => [cx + r * Math.sin(a), cy - r * Math.cos(a)];
  const [x0, y0] = p(a0);
  const [x1, y1] = p(a1);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)}A${r} ${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

export function ScopingRing({
  domains,
  breadthPct,
  breadthDelta,
  figures,
  hasAev,
  aevUrl,
  onConnect,
}: {
  domains: CtemAttackDomain[];
  breadthPct: number | null;
  breadthDelta: number | null;
  figures: { label: string; value: number | null }[];
  /** OpenAEV is connected; without domains the ring then says "no data", never "connect". */
  hasAev: boolean;
  aevUrl?: string;
  onConnect: () => void;
}) {
  const configured = hasAev && domains.length > 0;
  const { t_i18n } = useFormatter();
  const nf = useNumberFormat();
  const reduced = usePrefersReducedMotion();
  const roving = useRovingMarks(domains.length);
  const covered = domains.filter((d) => d.covered).length;
  const cx = RING_W / 2;
  const cy = RING_H / 2;
  const r = RING_R;
  const gap = 0.05;
  const step = domains.length > 0 ? (2 * Math.PI) / domains.length : 0;
  const markLabel = (d: CtemAttackDomain) =>
    d.covered
      ? t_i18n("{name}: covered, {count, plural, one {# contract} other {# contracts}}", { name: d.name, count: d.contracts })
      : t_i18n("{name}: untested", { name: d.name });

  return (
    <HeroCard title={t_i18n("Attack surface tested")} testId="ctem-hero-scoping">
      <div className="flex flex-wrap items-center gap-6">
        <svg
          width={RING_W}
          height={RING_H}
          viewBox={`0 0 ${RING_W} ${RING_H}`}
          role="group"
          aria-label={t_i18n("Attack surface tested")}
          tabIndex={0}
          onKeyDown={(e) => roving.onKeyDown(e)}
          className="shrink-0 max-w-full focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
        >
          <HeroPatterns id="ring" />
          {domains.length === 0 && (
            <circle cx={cx} cy={cy} r={r} fill="none" stroke="currentColor" strokeOpacity={0.35} strokeWidth={4} strokeDasharray="4 4" data-testid="ctem-ring-empty" />
          )}
          {domains.map((d, i) => {
            const a0 = i * step + gap;
            const a1 = (i + 1) * step - gap;
            const mid = (a0 + a1) / 2;
            const lx = cx + (r + 26) * Math.sin(mid);
            const ly = cy - (r + 26) * Math.cos(mid);
            const anchor = Math.abs(Math.sin(mid)) < 0.2 ? "middle" : Math.sin(mid) > 0 ? "start" : "end";
            const active = roving.active === i;
            return (
              <g key={d.name} aria-label={configured ? markLabel(d) : d.name} role="img" data-testid={`ctem-ring-${d.name}`}>
                <title>{configured ? markLabel(d) : d.name}</title>
                {!configured ? (
                  <path d={arcPath(cx, cy, r, a0, a1)} fill="none" stroke="currentColor" strokeOpacity={0.35} strokeWidth={4} strokeDasharray="4 4" />
                ) : d.covered ? (
                  <path
                    d={arcPath(cx, cy, r, a0, a1)}
                    fill="none"
                    stroke={SUCCESS}
                    strokeWidth={active ? 20 : 16}
                    style={reduced ? undefined : { transition: "opacity 400ms", opacity: 1 }}
                  />
                ) : (
                  <path d={arcPath(cx, cy, r, a0, a1)} fill="none" stroke="url(#ring-hatch)" strokeWidth={active ? 20 : 16} />
                )}
                {configured && !d.covered && (
                  <path d={arcPath(cx, cy, r + 8, a0, a1)} fill="none" stroke={DANGER} strokeWidth={1} />
                )}
                <text x={lx} y={ly} textAnchor={anchor} dominantBaseline="middle" fontSize={11} fill="currentColor">
                  {d.name}
                </text>
                {configured && !d.covered && (
                  <text x={lx} y={ly + 13} textAnchor={anchor} dominantBaseline="middle" fontSize={11} fill={DANGER}>
                    {t_i18n("untested")}
                  </text>
                )}
              </g>
            );
          })}
          <text x={cx} y={cy - 4} textAnchor="middle" dominantBaseline="middle" fontSize={26} fontWeight={700} fill="currentColor" data-testid="ctem-ring-center">
            {configured && breadthPct != null ? nf.percent(breadthPct / 100) : "--"}
          </text>
          {configured && breadthDelta != null && breadthDelta !== 0 && (
            <text
              x={cx}
              y={cy + 18}
              textAnchor="middle"
              fontSize={11}
              fill={breadthDelta > 0 ? SUCCESS : DANGER}
            >
              {breadthDelta > 0 ? `+${breadthDelta}` : String(breadthDelta)}
            </text>
          )}
        </svg>
        <div className="flex-1 min-w-[180px] space-y-3">
          {configured ? (
            <dl className="grid grid-cols-1 gap-3">
              {figures.map((f) => (
                <div key={f.label}>
                  <dt className="content-caption text-default-secondary">{f.label}</dt>
                  <dd className="text-xl font-semibold">
                    <CompactFigure exact={nf.exactCount(f.value)}>{nf.count(f.value)}</CompactFigure>
                  </dd>
                </div>
              ))}
            </dl>
          ) : hasAev ? (
            <NotConfiguredFrame
              reason={t_i18n("No attack domain in OpenAEV yet")}
              action={t_i18n("Open {name}", { name: "OpenAEV" })}
              href={aevUrl}
              onAction={aevUrl ? undefined : onConnect}
              testId="ctem-ring-no-data"
            />
          ) : (
            <NotConfiguredFrame
              reason={t_i18n("Attack domains need OpenAEV injector contracts")}
              action={t_i18n("Connect {name}", { name: "OpenAEV" })}
              onAction={onConnect}
              testId="ctem-ring-not-configured"
            />
          )}
        </div>
      </div>
      <HiddenChartValues
        caption={t_i18n("{covered} of {total, plural, one {# attack domain} other {# attack domains}} covered", { covered, total: domains.length })}
        head={[t_i18n("Domain"), t_i18n("Covered"), t_i18n("Contracts")]}
        rows={domains.map((d) => [d.name, d.covered ? t_i18n("Yes") : t_i18n("No"), d.contracts])}
      />
    </HeroCard>
  );
}

// ── Discovery: tested vs untested threat techniques over time (3.2) ─────────

export interface DiscoveryPoint {
  date: string;
  label: string;
  /** Techniques the tracked threats use. */
  threat: number | null;
  /** Of those, the ones OpenAEV simulates. */
  tested: number | null;
}

const AREA_HEIGHT = 160;

export function DiscoveryArea({
  points,
  headlinePct,
  headlineDelta,
  threatTracked,
  hasCti,
  hasAev,
  aevUrl,
  onConnect,
}: {
  points: DiscoveryPoint[];
  headlinePct: number | null;
  headlineDelta: number | null;
  threatTracked: number | null;
  /** Connected state from posture.products.<p>.present, never the stage's products_present (UX round 9, B1). */
  hasCti: boolean;
  hasAev: boolean;
  /** The connected OpenAEV's base URL, for the no-data action. */
  aevUrl?: string;
  onConnect: (product: "opencti" | "openaev") => void;
}) {
  const { t_i18n } = useFormatter();
  const nf = useNumberFormat();
  const { ref, width } = useElementWidth<HTMLDivElement>();
  const valid = points.filter((p) => p.threat != null && p.threat > 0 && p.tested != null);
  const shares = valid.map((p) => Math.min(1, (p.tested ?? 0) / (p.threat ?? 1)));
  const roving = useRovingMarks(valid.length);
  const w = Math.max(width, 240);
  const padL = 36;
  const padR = 56;
  const innerW = w - padL - padR;
  const y = (share: number) => 8 + (1 - share) * (AREA_HEIGHT - 28);
  const x = (i: number) => padL + (valid.length > 1 ? (i / (valid.length - 1)) * innerW : innerW / 2);

  // Design-system chips (UX round 8, B5): a source not connected says so in its title.
  const chips = (
    <>
      <Chip
        label={t_i18n("OpenCTI threats")}
        severity={hasCti ? "info" : "neutral"}
        title={hasCti ? t_i18n("OpenCTI threats") : t_i18n("Not connected")}
        data-testid="ctem-area-source-opencti"
      />
      <Chip
        label={t_i18n("OpenAEV simulations")}
        severity={hasAev ? "info" : "neutral"}
        title={hasAev ? t_i18n("OpenAEV simulations") : t_i18n("Not connected")}
        data-testid="ctem-area-source-openaev"
      />
    </>
  );

  let body: ReactNode;
  if (!hasCti) {
    body = (
      <NotConfiguredFrame
        reason={t_i18n("Threat techniques need OpenCTI threats")}
        action={t_i18n("Connect {name}", { name: "OpenCTI" })}
        onAction={() => onConnect("opencti")}
        testId="ctem-area-not-configured"
      />
    );
  } else if (!hasAev) {
    body = (
      <div className="space-y-3">
        <p className="text-2xl font-bold">
          <CompactFigure exact={nf.exactCount(threatTracked)}>{nf.count(threatTracked)}</CompactFigure>{" "}
          <span className="content-compact font-normal text-default-secondary">{t_i18n("threat techniques tracked")}</span>
        </p>
        <NotConfiguredFrame
          reason={t_i18n("Tested share needs OpenAEV simulations")}
          action={t_i18n("Connect {name}", { name: "OpenAEV" })}
          onAction={() => onConnect("openaev")}
          testId="ctem-area-needs-aev"
        />
      </div>
    );
  } else if (valid.length === 0 && headlinePct == null) {
    // Both products connected, no simulation result yet: a true no-data state, never "connect".
    body = (
      <div className="space-y-3">
        <p className="text-2xl font-bold">
          <CompactFigure exact={nf.exactCount(threatTracked)}>{nf.count(threatTracked)}</CompactFigure>{" "}
          <span className="content-compact font-normal text-default-secondary">{t_i18n("threat techniques tracked")}</span>
        </p>
        <NotConfiguredFrame
          reason={t_i18n("No simulation result in OpenAEV yet")}
          action={t_i18n("Open {name}", { name: "OpenAEV" })}
          href={aevUrl}
          onAction={aevUrl ? undefined : () => onConnect("openaev")}
          testId="ctem-area-no-data"
        />
      </div>
    );
  } else if (valid.length < 2) {
    const share = shares[0] ?? (headlinePct != null ? headlinePct / 100 : null);
    body = (
      <div className="space-y-2" data-testid="ctem-area-single">
        <div className="flex h-6 w-full overflow-hidden rounded-sm" role="img" aria-label={t_i18n("Tested share {value}", { value: nf.percent(share) })}>
          <div style={{ width: `${(share ?? 0) * 100}%`, background: SUCCESS }} />
          <svg className="flex-1" height="24" aria-hidden="true">
            <HeroPatterns id="bar" />
            <rect width="100%" height="24" fill="url(#bar-hatch)" />
          </svg>
        </div>
        <p className="content-caption text-default-secondary">{t_i18n("Trend after 2 days of history")}</p>
      </div>
    );
  } else {
    const top = valid.map((_, i) => `${x(i)},${y(shares[i])}`).join(" ");
    const tested = `M${x(0)},${y(0)} L${top.replace(/ /g, " L")} L${x(valid.length - 1)},${y(0)} Z`;
    const untested = `M${x(0)},${y(1)} L${x(valid.length - 1)},${y(1)} L${valid
      .map((_, i) => `${x(valid.length - 1 - i)},${y(shares[valid.length - 1 - i])}`)
      .join(" L")} Z`;
    const ticks = [0, Math.floor((valid.length - 1) / 2), valid.length - 1];
    const a = roving.active;
    body = (
      <svg
        width={w}
        height={AREA_HEIGHT}
        role="group"
        aria-label={t_i18n("Threat techniques tested")}
        tabIndex={0}
        onKeyDown={(e) => roving.onKeyDown(e)}
        className="block focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
      >
        <HeroPatterns id="area" />
        <path d={untested} fill="url(#area-hatch)" />
        <path d={tested} fill={SUCCESS} fillOpacity={0.85} />
        <text x={padL - 6} y={y(0)} textAnchor="end" fontSize={11} fill="currentColor">0</text>
        <text x={padL - 6} y={y(1) + 4} textAnchor="end" fontSize={11} fill="currentColor">100%</text>
        {ticks.map((i) => (
          <text key={i} x={x(i)} y={AREA_HEIGHT - 4} textAnchor="middle" fontSize={11} fill="currentColor">
            {valid[i].label}
          </text>
        ))}
        <text x={w - padR + 6} y={y(shares[shares.length - 1])} dominantBaseline="middle" fontSize={13} fontWeight={700} fill="currentColor">
          {nf.percent(shares[shares.length - 1])}
        </text>
        {headlineDelta != null && headlineDelta !== 0 && (
          <text x={w - padR + 6} y={y(shares[shares.length - 1]) + 15} fontSize={11} fill={headlineDelta > 0 ? SUCCESS : DANGER}>
            {headlineDelta > 0 ? `+${headlineDelta}` : String(headlineDelta)}
          </text>
        )}
        <g role="img" aria-label={`${valid[a].label}: ${nf.percent(shares[a])}`}>
          <line x1={x(a)} x2={x(a)} y1={y(1)} y2={y(0)} stroke="currentColor" strokeOpacity={0.5} />
          <title>
            {t_i18n("{day}: {tested} of {threat, plural, one {# technique} other {# techniques}} tested ({pct})", {
              day: valid[a].label,
              tested: valid[a].tested ?? 0,
              threat: valid[a].threat ?? 0,
              pct: nf.percent(shares[a]),
            })}
          </title>
        </g>
      </svg>
    );
  }

  return (
    <HeroCard title={t_i18n("Threat techniques tested")} chips={chips} testId="ctem-hero-discovery">
      <div ref={ref} className="w-full">
        {body}
      </div>
      {hasCti && hasAev && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 content-caption">
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-block h-2.5 w-2.5 rounded-sm" style={{ background: SUCCESS }} /> {t_i18n("Tested")}
          </span>
          <span className="inline-flex items-center gap-1.5">
            <svg width="10" height="10" aria-hidden="true">
              <HeroPatterns id="key" />
              <rect width="10" height="10" fill="url(#key-hatch)" />
            </svg>
            {t_i18n("Untested")}
          </span>
        </div>
      )}
      <HiddenChartValues
        caption={t_i18n("Threat techniques tested")}
        head={[t_i18n("Day"), t_i18n("Tested"), t_i18n("Threat techniques")]}
        rows={valid.map((p) => [p.label, p.tested ?? 0, p.threat ?? 0])}
      />
    </HeroCard>
  );
}

// ── Validation: outcome matrix, any framework (3.4) ──────────────────────────

const OUTCOME_ORDER: CtemMatrixOutcome[] = ["prevented", "detected", "missed", "untested"];
const MAX_ROWS = 8;

function outcomeLabel(t_i18n: (s: string) => string, o: CtemMatrixOutcome): string {
  return o === "prevented" ? t_i18n("Prevented") : o === "detected" ? t_i18n("Detected only") : o === "missed" ? t_i18n("Missed") : t_i18n("Untested");
}

function OutcomeSwatch({ outcome, size = 12 }: { outcome: CtemMatrixOutcome; size?: number }) {
  const id = `sw-${outcome}`;
  return (
    <svg width={size} height={size} aria-hidden="true" className="shrink-0">
      <HeroPatterns id={id} />
      {outcome === "prevented" && <rect width={size} height={size} rx={2} fill={SUCCESS} />}
      {outcome === "detected" && <rect width={size} height={size} rx={2} fill={`url(#${id}-diag)`} />}
      {outcome === "missed" && <rect width={size} height={size} rx={2} fill={`url(#${id}-cross)`} />}
      {outcome === "untested" && (
        <rect x={0.5} y={0.5} width={size - 1} height={size - 1} rx={2} fill="none" stroke="currentColor" strokeOpacity={0.6} strokeDasharray="2 2" />
      )}
    </svg>
  );
}

/** The columns of one chain: phases in order, each technique sorted by outcome. */
export function matrixColumns(matrix: CtemOutcomeMatrix, chainId: string | null) {
  const chain = matrix.chains.find((c) => c.id === chainId) ?? matrix.chains.find((c) => c.default) ?? matrix.chains[0];
  if (!chain) return { chain: null, columns: [] as { id: string; label: string; items: CtemMatrixItem[]; prevented: number }[] };
  const rank = (o: CtemMatrixOutcome) => OUTCOME_ORDER.indexOf(o);
  const columns = chain.groups.map((g) => {
    const items = matrix.items
      .filter((it) => it.phases.includes(g.id))
      .sort((a, b) => rank(a.outcome) - rank(b.outcome) || a.label.localeCompare(b.label));
    return { id: g.id, label: g.label, items, prevented: items.filter((it) => it.outcome === "prevented").length };
  });
  return { chain, columns };
}

export function ValidationMatrix({
  matrix,
  preventionPct,
  detectionPct,
  hasAev,
  scenariosUrl,
  techniqueUrl,
  onConnect,
  initialChain = null,
}: {
  matrix: CtemOutcomeMatrix | undefined;
  /** The chain the matrix opens on: the command center's context framework
   *  (dev-docs/ctem-stage-viz.md 8.2); null: the default chain. */
  initialChain?: string | null;
  preventionPct: number | null;
  detectionPct: number | null;
  hasAev: boolean;
  scenariosUrl?: string;
  techniqueUrl?: (item: CtemMatrixItem) => string | undefined;
  onConnect: () => void;
}) {
  const { t_i18n } = useFormatter();
  const nf = useNumberFormat();
  const [picked, setPicked] = useState<string | null>(initialChain);
  // "+N more" opens the whole column in place (UX round 8, B3): no dead link.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const rowsOf = (col: { id: string; items: CtemMatrixItem[] }) => (expanded.has(col.id) ? col.items : col.items.slice(0, MAX_ROWS));
  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const { chain, columns } = useMemo(() => (matrix ? matrixColumns(matrix, picked) : { chain: null, columns: [] }), [matrix, picked]);
  const cells = columns.flatMap((c) => rowsOf(c));
  const roving = useRovingMarks(cells.length);
  const chainName = chain ? frameworkLabel(chain.label) : "";
  const exercised = chain?.count ?? 0;

  const selector =
    matrix && matrix.chains.length > 0 ? (
      <Select value={chain?.id ?? ""} onValueChange={setPicked}>
        <SelectTrigger className="w-56" aria-label={t_i18n("Framework")} data-testid="ctem-framework-select">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {matrix.chains.map((c) => (
            <SelectItem key={c.id} value={c.id}>
              {c.count > 0 ? `${frameworkLabel(c.label)} · ${nf.count(c.count)}` : `${frameworkLabel(c.label)} · ${t_i18n("no data")}`}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    ) : null;

  const headline = (
    <div className="flex flex-wrap gap-6">
      <div>
        <p className="content-caption text-default-secondary">{t_i18n("Prevention")}</p>
        <p className="text-2xl font-bold">{preventionPct != null ? nf.percent(preventionPct / 100) : "--"}</p>
      </div>
      <div>
        <p className="content-caption text-default-secondary">{t_i18n("Detection")}</p>
        <p className="text-2xl font-bold">{detectionPct != null ? nf.percent(detectionPct / 100) : "--"}</p>
      </div>
    </div>
  );

  let body: ReactNode;
  if (hasAev && !matrix) {
    // Connected but nothing read yet: a no-data state, never "connect" (UX round 8, B2).
    body = (
      <NotConfiguredFrame
        reason={t_i18n("No outcome matrix from OpenAEV yet")}
        action={t_i18n("Open OpenAEV scenarios")}
        href={scenariosUrl}
        onAction={scenariosUrl ? undefined : onConnect}
        testId="ctem-matrix-no-data"
      />
    );
  } else if (!hasAev || !matrix) {
    body = (
      <NotConfiguredFrame
        reason={t_i18n("The outcome matrix needs OpenAEV simulations")}
        action={t_i18n("Connect {name}", { name: "OpenAEV" })}
        onAction={onConnect}
        testId="ctem-matrix-not-configured"
      />
    );
  } else if (!chain || exercised === 0) {
    body = (
      <div className="space-y-3" data-testid="ctem-matrix-no-simulation">
        <div className="flex gap-2 overflow-x-auto">
          {columns.map((c) => (
            <div key={c.id} className="min-w-[122px] h-24 rounded-sm border border-dashed border-elevation-subtle-soft p-2 content-caption text-default-secondary">
              {c.label}
            </div>
          ))}
        </div>
        <NotConfiguredFrame
          reason={t_i18n("No simulation follows {name} yet", { name: chainName || t_i18n("this framework") })}
          action={t_i18n("Open OpenAEV scenarios")}
          href={scenariosUrl}
          onAction={scenariosUrl ? undefined : onConnect}
        />
      </div>
    );
  } else {
    let index = -1;
    body = (
      <div className="relative">
        <div
          className="group overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-accent)]"
          role="grid"
          aria-label={t_i18n("Outcome by phase, {name}", { name: chainName })}
          tabIndex={0}
          onKeyDown={(e) =>
            roving.onKeyDown(e, (i) => {
              const url = safeExternalUrl(techniqueUrl?.(cells[i]));
              if (url) window.open(url, "_blank", "noopener,noreferrer");
            })
          }
          data-testid="ctem-matrix"
        >
          <div className="flex gap-2 min-w-max pb-2">
            {columns.map((col) => {
              const share = col.items.length > 0 ? col.prevented / col.items.length : null;
              return (
                <div key={col.id} className="w-[122px] shrink-0 space-y-1.5" role="row">
                  <div className="sticky top-0 space-y-1" role="columnheader">
                    <p className="content-caption font-medium line-clamp-3 min-h-[3lh]" title={col.label}>
                      {col.label}
                    </p>
                    <div
                      className="h-1.5 w-full rounded-full bg-elevation-hover overflow-hidden"
                      role="img"
                      aria-label={share == null ? t_i18n("No technique exercised") : t_i18n("Prevented {value}", { value: nf.percent(share) })}
                    >
                      <div className="h-full" style={{ width: `${(share ?? 0) * 100}%`, background: SUCCESS }} />
                    </div>
                  </div>
                  {rowsOf(col).map((it) => {
                    index += 1;
                    const i = index;
                    // Only an http(s) address is ever linked or opened (Aikido on #11549).
                    const url = safeExternalUrl(techniqueUrl?.(it));
                    const label = t_i18n("{technique}: {outcome}, prevention {ps}/{pt}, detection {ds}/{dt}", {
                      technique: it.code ? `${it.code} ${it.label}` : it.label,
                      outcome: outcomeLabel(t_i18n, it.outcome),
                      ps: it.prevention.success,
                      pt: it.prevention.total,
                      ds: it.detection.success,
                      dt: it.detection.total,
                    });
                    // The focus ring shows only while the grid has keyboard focus (A2).
                    const cellClass = cn(
                      "flex items-center gap-1.5 rounded-sm px-1.5 py-1 content-caption",
                      url && "hover:bg-elevation-hover",
                      roving.active === i && "group-focus-visible:ring-1 group-focus-visible:ring-[var(--color-accent)]",
                    );
                    const content = (
                      <>
                        <OutcomeSwatch outcome={it.outcome} />
                        <span className="truncate">{it.code || it.label}</span>
                      </>
                    );
                    // A cell is a link only when there is a page to open (UX round 8, B3).
                    return url ? (
                      <a
                        key={it.id}
                        role="gridcell"
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        tabIndex={-1}
                        aria-label={label}
                        title={label}
                        className={cellClass}
                        data-outcome={it.outcome}
                      >
                        {content}
                      </a>
                    ) : (
                      <div key={it.id} role="gridcell" aria-label={label} title={label} className={cellClass} data-outcome={it.outcome}>
                        {content}
                      </div>
                    );
                  })}
                  {col.items.length > MAX_ROWS && (
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-auto px-1.5 py-0.5 content-caption"
                      aria-expanded={expanded.has(col.id)}
                      onClick={() => toggle(col.id)}
                      data-testid={`ctem-matrix-more-${col.id}`}
                    >
                      {expanded.has(col.id) ? t_i18n("Show fewer") : t_i18n("+{count} more", { count: col.items.length - MAX_ROWS })}
                    </Button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
        <div className="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-[var(--bg-elevation-default)] to-transparent" aria-hidden="true" />
      </div>
    );
  }

  return (
    <HeroCard title={t_i18n("Outcome by phase")} chips={selector} testId="ctem-hero-validation">
      {hasAev && headline}
      {body}
      {hasAev && matrix && exercised > 0 && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 content-caption">
          {OUTCOME_ORDER.map((o) => (
            <span key={o} className="inline-flex items-center gap-1.5">
              <OutcomeSwatch outcome={o} /> {outcomeLabel(t_i18n, o)}
            </span>
          ))}
        </div>
      )}
      <HiddenChartValues
        caption={t_i18n("Outcome by phase, {name}", { name: chainName })}
        head={[t_i18n("Phase"), t_i18n("Technique"), t_i18n("Outcome")]}
        rows={columns.flatMap((c) => c.items.map((it) => [c.label, it.code || it.label, outcomeLabel(t_i18n, it.outcome)]))}
      />
    </HeroCard>
  );
}
