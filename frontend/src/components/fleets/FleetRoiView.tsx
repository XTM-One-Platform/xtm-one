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
 * The fleet page's "ROI" view (#9785, slice O8b of #9206; design
 * dev-docs/roi-outcomes.md 6.3 "Per fleet" and the states table): what the
 * fleet's agents delivered over the page's shared window (`?window=`, the
 * same value `FleetCostCard` reads), its three KPI cards, one row per agent,
 * the assumptions and the fleet's CSV / PDF export. Shown to the fleet's ROI
 * readers (administrators and its managers, #9350); both modes. Reading it
 * never starts a run nor calls a model.
 */
import { useMemo } from "react";
import { Clock3, Target } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { RoiExportButtons } from "@/components/roi/RoiExportButtons";
import { RoiChatSeriesCard } from "@/components/roi/RoiChatSeriesCard";
import {
  RoiAssumptions,
  RoiBreakdown,
  RoiEstimatedNote,
  RoiKpiCards,
  RoiReportSkeleton,
  RoiResetNote,
  RoiUnderK,
} from "@/components/roi/RoiReportParts";
import { useFleetRoiReport } from "@/hooks/api/useRoiReportApi";
import { useFleetWindow } from "@/hooks/useFleetWindow";
import { useFormatter } from "@/i18n";
import { FLEET_WINDOW_LABELS, FLEET_WINDOWS, widerFleetWindow } from "@/lib/fleet-roi-window";
import { fleetReviewPath, fleetRoiState, ledgerAgentRows, outcomeRows, scopeHeroFigures, scopeUnderK } from "@/lib/roi-outcomes";
import { valueIsEmpty } from "@/lib/roi-value";
import { useNavigate } from "react-router-dom";

interface FleetRoiViewProps {
  fleetId: string;
  /** Opens the fleet's settings, where its default unit of work is set; absent for a reader who cannot edit the fleet. */
  onSetFleetDefault?: () => void;
}

export function FleetRoiView({ fleetId, onSetFleetDefault }: FleetRoiViewProps) {
  const { t_i18n } = useFormatter();
  const [window, setWindow] = useFleetWindow();
  const navigate = useNavigate();
  const report = useFleetRoiReport(fleetId, window);
  // Read only when the window counted nothing: tells "never measured" from "nothing in this period".
  // Emptiness is the ledger's: a chat-only fleet has value without a run (#12077 L6b3 UX r1 B2).
  // Under k the k rule, not an empty window, explains a hidden hero: no lifetime read.
  const windowEmpty = !!report.data && !scopeUnderK(report.data) && valueIsEmpty(report.data);
  const lifetime = useFleetRoiReport(fleetId, "all", { enabled: windowEmpty });
  // The fleet's agents: the report's own fleet row is the KPI cards above. The ledger's By agent when the
  // answer carries it, its no-agent and folded rows summed last, so the table sums to the hero (#12077 L6b3 UX r2 B6).
  const table = useMemo(
    () => ledgerAgentRows(report.data) ?? { rows: outcomeRows(report.data).filter((row) => row.kind === "agent"), remainders: [] },
    [report.data],
  );
  const currency = report.data?.currency ?? "USD";

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <SegmentedFilter
        options={FLEET_WINDOWS.map((key) => ({ key, label: FLEET_WINDOW_LABELS[key] }))}
        value={window}
        onChange={setWindow}
        size="sm"
        selection="radio"
        ariaLabel={t_i18n("Window")}
        className="w-fit"
      />
      <RoiExportButtons window={window} fleetId={fleetId} />
    </div>
  );

  if (report.isLoading) {
    return (
      <div className="space-y-4" aria-busy="true" data-testid="fleet-roi-view">
        {header}
        <RoiReportSkeleton />
      </div>
    );
  }

  if (report.isError || !report.data) {
    return (
      <div className="space-y-4" data-testid="fleet-roi-view">
        {header}
        <div role="alert" className="flex flex-wrap items-center gap-3 content-compact text-feedback-error-primary">
          {t_i18n("The ROI of this fleet could not be read.")}
          <Button type="button" variant="outline" size="sm" onClick={() => report.refetch()}>
            {t_i18n("Retry")}
          </Button>
        </div>
      </div>
    );
  }

  // Fewer than k people used the fleet (design 12.5): the viewer's own share, or no hero at all.
  const underK = scopeUnderK(report.data);
  if (underK && !underK.viewerUsed) {
    return (
      <div className="space-y-4" data-testid="fleet-roi-view">
        {header}
        <RoiUnderK kind="fleet" {...underK} />
      </div>
    );
  }
  const state = fleetRoiState(report.data, lifetime.isError ? report.data : lifetime.data);
  if (state === null) {
    return (
      <div className="space-y-4" aria-busy="true" data-testid="fleet-roi-view">
        {header}
        <RoiReportSkeleton />
      </div>
    );
  }
  // The fleet's chat deliverables: the chat the ledger stamps with this fleet, the Chat line the hero sums (#12077).
  const chat = <RoiChatSeriesCard scope={{ kind: "fleet", id: fleetId }} window={window} urlKey="chat_" />;
  if (state !== "data") {
    const wider = widerFleetWindow(window);
    return (
      <div className="space-y-4" data-testid="fleet-roi-view">
        {header}
        <RoiResetNote since={report.data.value_measured_since} />
        {chat}
        {state === "none" ? (
          <EmptyState
            icon={Target}
            title="This fleet measures nothing yet."
            description="Pick a unit of work for the fleet or one of its agents."
            actionLabel={onSetFleetDefault ? "Set the fleet default" : undefined}
            actionIcon={Target}
            onAction={onSetFleetDefault}
          />
        ) : (
          <EmptyState
            icon={Clock3}
            title="Nothing was counted in this period."
            actionLabel={wider ? "Widen the window" : undefined}
            actionIcon={Clock3}
            onAction={wider ? () => setWindow(wider) : undefined}
          />
        )}
      </div>
    );
  }

  // One figure with the Value page, the list and the export (invariant d): the ledger's totals.
  const figures = scopeHeroFigures(report.data);
  if (underK) {
    // The viewer's own share: no per-agent row, no scope-wide step or assumption.
    return (
      <div className="space-y-4" data-testid="fleet-roi-view">
        {header}
        <RoiResetNote since={report.data.value_measured_since} />
        <RoiUnderK kind="fleet" {...underK} />
        <RoiKpiCards figures={figures} currency={currency} window={window} costMasked={report.data.cost_masked} />
        {chat}
      </div>
    );
  }
  return (
    <div className="space-y-4" data-testid="fleet-roi-view">
      {header}
      <RoiResetNote since={report.data.value_measured_since} />
      {report.data.estimated && <RoiEstimatedNote />}
      <RoiKpiCards
        figures={figures}
        currency={currency}
        window={window}
        costMasked={report.data.cost_masked}
        onReview={() => navigate(fleetReviewPath(fleetId, window))}
      />
      <RoiBreakdown
        rows={table.rows}
        remainders={table.remainders}
        currency={currency}
        withKind={false}
        urlKey="roi_"
        storageKey="fleet-roi"
        searchPlaceholder={t_i18n("Search agents...")}
        noFilters="One row per agent of the fleet: the search is the only narrowing it needs."
      />
      <RoiAssumptions facts={figures.assumption_facts ?? []} computation={figures.computation ?? []} />
      {chat}
    </div>
  );
}
