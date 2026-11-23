// SPDX-License-Identifier: Apache-2.0
/**
 * The mixed views of the Discovery stage page (#11998, design round 5 approved):
 * what each connected module already holds, drawn, and one gap filler card per
 * missing module, so no module combination leaves an empty page or an empty frame.
 *
 * Every figure comes from the stage's viz payload (backend `_stage_viz`); a list
 * OpenCTI did not answer is not drawn. A gap filler shows an outline marked
 * Sample (never figures presented as real) and the one Connect action.
 */
import type { ReactElement } from "react";
import { Button } from "@/components/ui/button";
import { SampleBadge } from "@/components/ui/sample-badge";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CtemNamedCount, CtemStageViz, CtemTacticCount } from "@/types";
import { HeroCard, HiddenChartValues } from "./stage-heroes";
import { tacticLabel } from "./tactic-label";

/**
 * One horizontal bar per row, scaled to the largest value (true to scale, no log).
 * The bar track has a 4rem floor so a narrow card shrinks the label, never the bar
 * (UX round 2 of #12027, B1).
 */
export function RankedBars({ rows, testId }: { rows: { label: string; value: number }[]; testId: string }) {
  const { count, exactCount } = useNumberFormat();
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <ul className="space-y-2" data-testid={testId}>
      {rows.map((r) => (
        <li key={r.label} className="grid grid-cols-[minmax(0,10rem)_minmax(4rem,1fr)_3rem] items-center gap-3 content-compact">
          <span className="truncate text-default-secondary" title={r.label}>
            {r.label}
          </span>
          <span className="h-2 rounded-full bg-elevation-hover overflow-hidden" aria-hidden="true">
            <span className="block h-full rounded-full bg-accent" style={{ width: `${(100 * r.value) / max}%` }} />
          </span>
          <span className="tabular-nums text-right" title={exactCount(r.value)}>
            {count(r.value)}
          </span>
        </li>
      ))}
    </ul>
  );
}

const named = (rows: CtemNamedCount[] | undefined) => (rows ?? []).map((r) => ({ label: r.name, value: r.value }));

export type CtemModule = "OpenCTI" | "OpenAEV" | "OpenCRQ";

/**
 * What a missing module adds on a stage page: why, what it draws, an outline marked
 * Sample (never figures presented as real) and the one Connect action.
 */
export function StageGapFiller({
  module,
  why,
  adds,
  testId,
  onConnect,
}: {
  module: CtemModule;
  why: string;
  adds: string[];
  testId: string;
  onConnect: () => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    // A flex column whose Sample and Connect rows sit at the bottom (mt-auto), so the
    // Connect buttons of a gap row align whatever the copy length (UX round 1 of #12076, B3).
    <HeroCard title={t_i18n("What {module} adds here", { module })} testId={testId} className="flex flex-col">
      <p className="content-compact text-default-secondary">{why}</p>
      <ul className="list-disc pl-5 content-compact space-y-1">
        {adds.map((a) => (
          <li key={a}>{a}</li>
        ))}
      </ul>
      <div className="mt-auto space-y-4" data-testid={`${testId}-foot`}>
        <div className="relative rounded-sm border border-dashed border-elevation-subtle-soft p-3" aria-hidden="true">
          <SampleBadge className="absolute right-2 top-2" />
          <div className="flex h-16 items-end gap-1.5">
            {[40, 65, 30, 80, 55, 70, 45].map((h, i) => (
              <span key={i} className="flex-1 rounded-sm bg-elevation-hover" style={{ height: `${h}%` }} />
            ))}
          </div>
        </div>
        <div className="flex justify-end">
          <Button size="sm" variant="outline" onClick={onConnect}>
            {t_i18n("Connect {module}", { module })}
          </Button>
        </div>
      </div>
    </HeroCard>
  );
}

/** What a missing module adds on Discovery. */
function DiscoveryGapFiller({ module, onConnect }: { module: CtemModule; onConnect: () => void }) {
  const { t_i18n } = useFormatter();
  const copy: Record<CtemModule, { why: string; adds: string[] }> = {
    OpenCTI: {
      why: t_i18n("OpenCTI says which techniques the threats you face use."),
      adds: [t_i18n("Techniques by tactic, weighted by the tracked threats"), t_i18n("Top threats, sectors and countries targeting you")],
    },
    OpenAEV: {
      why: t_i18n("OpenAEV says which of these techniques you have tested."),
      adds: [t_i18n("Tested share per tactic"), t_i18n("Threat-informed coverage over 30 or 90 days, and the stage score")],
    },
    OpenCRQ: {
      why: t_i18n("OpenCRQ says what the threats you track could cost you."),
      adds: [t_i18n("Exposure in EUR of the top threat scenarios"), t_i18n("Share of the total expected loss per scenario")],
    },
  };
  const { why, adds } = copy[module];
  return <StageGapFiller module={module} why={why} adds={adds} testId={`ctem-discovery-gap-${module.toLowerCase()}`} onConnect={onConnect} />;
}

/**
 * The Discovery cards under the hero: OpenCTI's techniques by tactic, top threats
 * and targeting when OpenCTI answered, else its gap filler; a gap filler for
 * OpenAEV and OpenCRQ when they are not connected.
 */
export function DiscoveryMix({
  viz,
  connected,
  onConnect,
}: {
  viz: CtemStageViz | undefined;
  connected: { opencti: boolean; openaev: boolean; opencrq: boolean };
  onConnect: () => void;
}) {
  const { t_i18n } = useFormatter();
  const tactics: CtemTacticCount[] = viz?.techniques_by_tactic ?? [];
  const threats = named(viz?.top_threats);
  const sectors = named(viz?.target_sectors);
  const countries = named(viz?.target_countries);
  let tacticsCard: ReactElement | null = null;
  const sideCards: ReactElement[] = [];
  const gapCards: ReactElement[] = [];

  if (connected.opencti && tactics.length > 0) {
    const rows = tactics.map((r) => ({ label: tacticLabel(r.tactic), value: r.count }));
    tacticsCard = (
      <HeroCard key="tactics" title={t_i18n("Threat techniques by tactic")} testId="ctem-discovery-tactics">
        <RankedBars rows={rows} testId="ctem-discovery-tactics-bars" />
        <p className="content-compact text-default-secondary">
          {t_i18n("Distinct techniques the tracked threats use, per ATT&CK tactic (OpenCTI)")}
        </p>
        <HiddenChartValues caption={t_i18n("Threat techniques by tactic")} head={[t_i18n("Tactic"), t_i18n("Techniques")]} rows={rows.map((r) => [r.label, r.value])} />
      </HeroCard>
    );
  }
  if (connected.opencti && threats.length > 0) {
    sideCards.push(
      <HeroCard key="threats" title={t_i18n("Top threats by technique count")} testId="ctem-discovery-threats">
        <RankedBars rows={threats} testId="ctem-discovery-threats-bars" />
      </HeroCard>,
    );
  }
  if (connected.opencti && (sectors.length > 0 || countries.length > 0)) {
    sideCards.push(
      <HeroCard key="targeting" title={t_i18n("Targeting by the tracked threats")} testId="ctem-discovery-targeting">
        <div className="grid gap-5 2xl:grid-cols-2">
          {sectors.length > 0 && (
            <div className="space-y-2">
              <h3 className="content-compact font-semibold text-default-secondary">{t_i18n("Sectors")}</h3>
              <RankedBars rows={sectors} testId="ctem-discovery-sectors-bars" />
            </div>
          )}
          {countries.length > 0 && (
            <div className="space-y-2">
              <h3 className="content-compact font-semibold text-default-secondary">{t_i18n("Countries")}</h3>
              <RankedBars rows={countries} testId="ctem-discovery-countries-bars" />
            </div>
          )}
        </div>
      </HeroCard>,
    );
  }
  if (!connected.opencti) gapCards.push(<DiscoveryGapFiller key="gap-cti" module="OpenCTI" onConnect={onConnect} />);
  if (!connected.openaev) gapCards.push(<DiscoveryGapFiller key="gap-aev" module="OpenAEV" onConnect={onConnect} />);
  if (!connected.opencrq) gapCards.push(<DiscoveryGapFiller key="gap-crq" module="OpenCRQ" onConnect={onConnect} />);

  const hasData = tacticsCard !== null || sideCards.length > 0;
  if (!hasData && gapCards.length === 0) return null;
  // Below 2xl the cards stack on one column; from 2xl the tactics sit beside top threats
  // and targeting. Every card of a row ends on the tallest edge (Sam, 24 Nov 2026, #1612:
  // equal heights as in the rest of the app): the tactics card fills its cell and the last
  // side card grows to the column's end, content top-aligned. The gap fillers keep a row of
  // their own; a lone gap filler takes the full row.
  const twoColumns = tacticsCard !== null && sideCards.length > 0;
  return (
    <div className="space-y-5" data-testid="ctem-discovery-mix">
      {hasData && (
        <div
          className={cn("grid gap-5 [&>section]:h-full", twoColumns && "2xl:grid-cols-2")}
          data-testid="ctem-discovery-data-row"
          data-align="stretch"
          data-wide-columns={twoColumns ? 2 : 1}
        >
          {tacticsCard}
          {sideCards.length > 0 && (
            <div className="flex flex-col gap-5 [&>section:last-child]:grow" data-testid="ctem-discovery-side-column">
              {sideCards}
            </div>
          )}
        </div>
      )}
      {gapCards.length > 0 && (
        <div
          className={cn(
            "grid gap-5 [&>section]:h-full",
            gapCards.length >= 3 && "md:grid-cols-3",
            gapCards.length === 2 && "md:grid-cols-2",
          )}
          data-testid="ctem-discovery-gap-row"
          data-columns={Math.min(gapCards.length, 3)}
        >
          {gapCards}
        </div>
      )}
    </div>
  );
}
