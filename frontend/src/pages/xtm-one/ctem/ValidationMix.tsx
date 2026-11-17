// SPDX-License-Identifier: Apache-2.0
/**
 * The mixed views of the Validation stage page (#11998, design round 5 approved):
 * OpenCTI's detection rule coverage per ATT&CK tactic and its detection content,
 * beside OpenAEV's outcome matrix (the stage hero), and one gap filler card per
 * missing module, so no module combination leaves an empty page or an empty frame.
 *
 * Every figure comes from the stage's viz payload (backend `_stage_viz`); a list a
 * module did not answer is not drawn.
 */
import type { ReactElement } from "react";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CtemStageViz, CtemTacticCoverage } from "@/types";
import { HeroCard, HiddenChartValues } from "./stage-heroes";
import { RankedBars, StageGapFiller, type CtemModule } from "./DiscoveryMix";
import { tacticLabel } from "./tactic-label";

/** Each tactic's threat techniques, the share with an indicator or a detection rule filled, true to scale. */
function RuleCoverageRows({ rows }: { rows: CtemTacticCoverage[] }) {
  const { t_i18n } = useFormatter();
  const { count, exactCount } = useNumberFormat();
  const max = Math.max(1, ...rows.map((r) => r.total));
  return (
    <ul className="space-y-2" data-testid="ctem-validation-rule-rows">
      {rows.map((r) => {
        const covered = Math.min(r.covered, r.total);
        return (
          <li key={r.tactic} className="grid grid-cols-[minmax(0,10rem)_minmax(4rem,1fr)_5rem] items-center gap-3 content-compact">
            <span className="truncate text-default-secondary" title={tacticLabel(r.tactic)}>
              {tacticLabel(r.tactic)}
            </span>
            <span className="h-2 rounded-full bg-elevation-hover overflow-hidden" aria-hidden="true" style={{ width: `${(100 * r.total) / max}%` }}>
              <span className="block h-full rounded-full bg-accent" style={{ width: `${r.total > 0 ? (100 * covered) / r.total : 0}%` }} />
            </span>
            <span className="tabular-nums text-right" title={`${exactCount(covered)} / ${exactCount(r.total)}`}>
              {t_i18n("{covered} of {total}", { covered: count(covered), total: count(r.total) })}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** The Validation cards under the hero, then a gap filler per missing module. */
export function ValidationMix({
  viz,
  connected,
  onConnect,
}: {
  viz: CtemStageViz | undefined;
  connected: { opencti: boolean; openaev: boolean; opencrq: boolean };
  onConnect: () => void;
}) {
  const { t_i18n } = useFormatter();
  const depthLabels: Record<string, string> = {
    indicators: t_i18n("Indicators"),
    detection_indicators: t_i18n("Detection-ready indicators"),
    sightings: t_i18n("Indicator sightings"),
  };
  const coverage = (viz?.rule_coverage_by_tactic ?? []).filter((r) => r.total > 0);
  const depth = (viz?.detection_depth ?? []).filter((r) => depthLabels[r.name]).map((r) => ({ label: depthLabels[r.name], value: r.value }));
  const dataCards: ReactElement[] = [];
  const gapCards: ReactElement[] = [];

  if (connected.opencti && coverage.length > 0) {
    dataCards.push(
      <HeroCard key="rules" title={t_i18n("Detection rule coverage by tactic")} testId="ctem-validation-rules">
        {/* The sr-only table goes first, never last: the last child of the space-y card keeps a phantom margin (UX round 3 of #12096). */}
        <HiddenChartValues
          caption={t_i18n("Detection rule coverage by tactic")}
          head={[t_i18n("Tactic"), t_i18n("Covered"), t_i18n("Techniques")]}
          rows={coverage.map((r) => [tacticLabel(r.tactic), Math.min(r.covered, r.total), r.total])}
        />
        <RuleCoverageRows rows={coverage} />
        <p className="content-compact text-default-secondary">
          {t_i18n("Threat techniques with an indicator or a detection rule in OpenCTI, per ATT&CK tactic")}
        </p>
      </HeroCard>,
    );
  }
  if (connected.opencti && depth.length > 0) {
    dataCards.push(
      <HeroCard key="depth" title={t_i18n("Detection content")} testId="ctem-validation-depth">
        <HiddenChartValues caption={t_i18n("Detection content")} head={[t_i18n("Type"), t_i18n("Count")]} rows={depth.map((r) => [r.label, r.value])} />
        <RankedBars rows={depth} testId="ctem-validation-depth-bars" />
        <p className="content-compact text-default-secondary">{t_i18n("What OpenCTI holds to detect the tracked threats")}</p>
      </HeroCard>,
    );
  }

  const copy: Record<CtemModule, { why: string; adds: string[] }> = {
    OpenCTI: {
      why: t_i18n("OpenCTI says which of the techniques you face carry a detection rule."),
      adds: [t_i18n("Techniques with a detection rule, per tactic"), t_i18n("Indicators, detection-ready indicators and sightings")],
    },
    OpenAEV: {
      why: t_i18n("OpenAEV proves whether your defenses stop and detect these techniques."),
      adds: [t_i18n("Prevented and detected share per kill chain phase"), t_i18n("Prevention and detection success, and the stage score")],
    },
    OpenCRQ: {
      why: t_i18n("OpenCRQ shows which controls reduce risk the most."),
      adds: [t_i18n("Controls by share of risk reduction"), t_i18n("Loss avoided by validated controls in EUR")],
    },
  };
  const missing: CtemModule[] = [];
  if (!connected.opencti) missing.push("OpenCTI");
  if (!connected.openaev) missing.push("OpenAEV");
  if (!connected.opencrq) missing.push("OpenCRQ");
  for (const module of missing) {
    gapCards.push(
      <StageGapFiller
        key={module}
        module={module}
        why={copy[module].why}
        adds={copy[module].adds}
        testId={`ctem-validation-gap-${module.toLowerCase()}`}
        onConnect={onConnect}
      />,
    );
  }

  if (dataCards.length === 0 && gapCards.length === 0) return null;
  // The data cards stack on one column at every width: side by side, the rule coverage
  // (one row per tactic) ends far below the detection content (three bars), and the
  // bottoms must not drift apart (UX round 1 of #12142, B1, Design standards s.4).
  return (
    <div className="space-y-5" data-testid="ctem-validation-mix">
      {dataCards.length > 0 && (
        <div className="grid items-start gap-5" data-testid="ctem-validation-data-row" data-wide-columns={1}>
          {dataCards}
        </div>
      )}
      {gapCards.length > 0 && (
        <div
          className={cn(
            "grid gap-5 [&>section]:h-full",
            gapCards.length >= 3 && "md:grid-cols-3",
            gapCards.length === 2 && "md:grid-cols-2",
          )}
          data-testid="ctem-validation-gap-row"
          data-columns={Math.min(gapCards.length, 3)}
        >
          {gapCards}
        </div>
      )}
    </div>
  );
}
