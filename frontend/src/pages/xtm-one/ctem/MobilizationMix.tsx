// SPDX-License-Identifier: Apache-2.0
/**
 * The mixed views of the Mobilization stage page (#11998, design round 5 approved):
 * OpenCTI's response progress (cases closed, playbooks running) and its response
 * cases by kind, OpenAEV's fix backlog (what its simulations still find open), and
 * one gap filler card per missing module, so no module combination leaves an empty
 * page or an empty frame.
 *
 * Every figure comes from the stage's viz payload (backend `_stage_viz`); a list a
 * module did not answer is not drawn.
 */
import type { ReactElement } from "react";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CtemStageViz } from "@/types";
import { HeroCard, HiddenChartValues } from "./stage-heroes";
import { RankedBars, StageGapFiller, type CtemModule } from "./DiscoveryMix";

interface ProgressRow {
  key: string;
  label: string;
  done: number;
  total: number;
}

/** Each row's done share of its total, filled true to scale. */
function ProgressRows({ rows }: { rows: ProgressRow[] }) {
  const { t_i18n } = useFormatter();
  const { count, exactCount } = useNumberFormat();
  return (
    <ul className="space-y-2" data-testid="ctem-mobilization-progress-rows">
      {rows.map((r) => (
        <li key={r.key} className="grid grid-cols-[minmax(0,10rem)_minmax(4rem,1fr)_5rem] items-center gap-3 content-compact">
          <span className="truncate text-default-secondary" title={r.label}>
            {r.label}
          </span>
          <span className="h-2 rounded-full bg-elevation-hover overflow-hidden" aria-hidden="true">
            <span className="block h-full rounded-full bg-accent" style={{ width: `${(100 * r.done) / r.total}%` }} />
          </span>
          <span className="tabular-nums text-right" title={`${exactCount(r.done)} / ${exactCount(r.total)}`}>
            {t_i18n("{covered} of {total}", { covered: count(r.done), total: count(r.total) })}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** The Mobilization cards under the Cases board, then a gap filler per missing module. */
export function MobilizationMix({
  viz,
  connected,
  onConnect,
}: {
  viz: CtemStageViz | undefined;
  connected: { opencti: boolean; openaev: boolean; opencrq: boolean };
  onConnect: () => void;
}) {
  const { t_i18n } = useFormatter();
  const mixLabels: Record<string, string> = {
    case_incidents: t_i18n("Incident response cases"),
    case_rfis: t_i18n("Requests for information"),
  };
  const backlogLabels: Record<string, string> = {
    open_findings: t_i18n("Open findings"),
    failed_partial_expectations: t_i18n("Failed or partial expectations"),
  };
  const named = (rows: { name: string; value: number }[] | undefined, labels: Record<string, string>) =>
    (rows ?? []).filter((r) => labels[r.name] && r.value > 0).map((r) => ({ label: labels[r.name], value: r.value }));
  const mix = named(viz?.response_mix, mixLabels);
  const backlog = named(viz?.fix_backlog, backlogLabels);
  const p = viz?.response_progress ?? null;
  const progress: ProgressRow[] = [];
  if (p?.cases_total && p.cases_total > 0 && p.cases_closed != null) {
    progress.push({ key: "cases", label: t_i18n("Response cases closed"), done: Math.min(p.cases_closed, p.cases_total), total: p.cases_total });
  }
  if (p?.playbooks_total && p.playbooks_total > 0 && p.playbooks_running != null) {
    progress.push({ key: "playbooks", label: t_i18n("Playbooks running"), done: Math.min(p.playbooks_running, p.playbooks_total), total: p.playbooks_total });
  }
  const dataCards: ReactElement[] = [];
  const gapCards: ReactElement[] = [];

  if (connected.opencti && progress.length > 0) {
    dataCards.push(
      <HeroCard key="progress" title={t_i18n("Response progress")} testId="ctem-mobilization-progress">
        {/* The sr-only table goes first, never last: the last child of the space-y card keeps a phantom margin (UX round 3 of #12096). */}
        <HiddenChartValues
          caption={t_i18n("Response progress")}
          head={[t_i18n("Measure"), t_i18n("Count"), t_i18n("Total")]}
          rows={progress.map((r) => [r.label, r.done, r.total])}
        />
        <ProgressRows rows={progress} />
        <p className="content-compact text-default-secondary">{t_i18n("Closed response cases and enabled playbooks in OpenCTI")}</p>
      </HeroCard>,
    );
  }
  if (connected.opencti && mix.length > 0) {
    dataCards.push(
      <HeroCard key="mix" title={t_i18n("Response cases by kind")} testId="ctem-mobilization-mix-cases">
        <HiddenChartValues caption={t_i18n("Response cases by kind")} head={[t_i18n("Type"), t_i18n("Count")]} rows={mix.map((r) => [r.label, r.value])} />
        <RankedBars rows={mix} testId="ctem-mobilization-mix-bars" />
        <p className="content-compact text-default-secondary">{t_i18n("The response cases OpenCTI tracks, by kind")}</p>
      </HeroCard>,
    );
  }
  if (connected.openaev && backlog.length > 0) {
    dataCards.push(
      <HeroCard key="backlog" title={t_i18n("Fix backlog")} testId="ctem-mobilization-backlog">
        <HiddenChartValues caption={t_i18n("Fix backlog")} head={[t_i18n("Type"), t_i18n("Count")]} rows={backlog.map((r) => [r.label, r.value])} />
        <RankedBars rows={backlog} testId="ctem-mobilization-backlog-bars" />
        <p className="content-compact text-default-secondary">{t_i18n("What OpenAEV simulations still find open")}</p>
      </HeroCard>,
    );
  }

  const copy: Record<CtemModule, { why: string; adds: string[] }> = {
    OpenCTI: {
      why: t_i18n("OpenCTI tracks the response work: cases, requests and playbooks."),
      adds: [t_i18n("Response cases closed and playbooks running"), t_i18n("Response cases by kind")],
    },
    OpenAEV: {
      why: t_i18n("OpenAEV re-tests the fixes and says what is still open."),
      adds: [t_i18n("Open findings and failed or partial expectations"), t_i18n("Prevention success, part of the stage score")],
    },
    OpenCRQ: {
      why: t_i18n("OpenCRQ says which fixes reduce the most risk."),
      adds: [t_i18n("Expected loss in EUR reduced per fix"), t_i18n("Fixes ranked by risk reduction")],
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
        testId={`ctem-mobilization-gap-${module.toLowerCase()}`}
        onConnect={onConnect}
      />,
    );
  }

  if (dataCards.length === 0 && gapCards.length === 0) return null;
  // The data cards stack on one column at every width, as on Validation: rows of
  // different heights side by side leave their bottoms apart (UX round 1 of #12142, B1).
  return (
    <div className="space-y-5" data-testid="ctem-mobilization-mix">
      {dataCards.length > 0 && (
        <div className="grid items-start gap-5" data-testid="ctem-mobilization-data-row" data-wide-columns={1}>
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
          data-testid="ctem-mobilization-gap-row"
          data-columns={Math.min(gapCards.length, 3)}
        >
          {gapCards}
        </div>
      )}
    </div>
  );
}
