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
 * The agent's ROI section, first in its Value tab since #11037 (#9807, slice O8c of #9206; design
 * dev-docs/roi-outcomes.md 6.3 "Per agent (C2)"): the three KPI cards every
 * ROI view draws (hours saved, net value with cost, review rate), scoped to
 * the agent over the page's `?window=` (7 / 30 / 90 days, the fleet page's
 * control). The review-rate card opens the agent's unreviewed runs; its
 * unreviewed work items, which that list never shows, are counted apart. The
 * shared Assumptions block under the cards says how every figure is computed
 * (design standards s6). Shown to every reader of the agent (the agent scope
 * of `GET /roi` follows the agent's visibility); both modes.
 * Reading it never starts a run nor calls a model.
 */
import { useNavigate } from "react-router-dom";
import { Clock3, Target } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { RoiChatSeriesCard } from "@/components/roi/RoiChatSeriesCard";
import { RoiAssumptions, RoiEstimatedNote, RoiKpiCards, RoiReportSkeleton, RoiUnderK } from "@/components/roi/RoiReportParts";
import { useAgentRoiReport } from "@/hooks/api/useRoiReportApi";
import { useFleetWindow } from "@/hooks/useFleetWindow";
import { useFormatter } from "@/i18n";
import { FLEET_WINDOW_LABELS, FLEET_WINDOWS, widerFleetWindow } from "@/lib/fleet-roi-window";
import { agentPickUnitPath, agentReviewPath, fleetRoiState, scopeHeroFigures, scopeUnderK } from "@/lib/roi-outcomes";
import { valueIsEmpty } from "@/lib/roi-value";

export function AgentRoiSection({ agentId, canManage }: { agentId: string; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const [window, setWindow] = useFleetWindow();
  const report = useAgentRoiReport(agentId, window);
  // Read only when the window counted nothing: tells "never counted" from "nothing in this period".
  // Emptiness is the ledger's: a chat-only agent has value without a run (#12077 L6b3 UX r1 B2).
  // Under k the k rule, not an empty window, explains a hidden hero: no lifetime read.
  const windowEmpty = !!report.data && !scopeUnderK(report.data) && valueIsEmpty(report.data);
  const lifetime = useAgentRoiReport(agentId, "all", { enabled: windowEmpty });

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 id="agent-roi-title" className="content-base font-semibold">
        {t_i18n("ROI")}
      </h2>
      <SegmentedFilter
        options={FLEET_WINDOWS.map((key) => ({ key, label: FLEET_WINDOW_LABELS[key] }))}
        value={window}
        onChange={setWindow}
        size="sm"
        selection="radio"
        ariaLabel={t_i18n("Window")}
        className="w-fit"
      />
    </div>
  );

  const body = (() => {
    if (report.isLoading) return <RoiReportSkeleton />;
    if (report.isError || !report.data) {
      return (
        <div role="alert" className="flex flex-wrap items-center gap-3 content-compact text-feedback-error-primary">
          {t_i18n("The ROI of this agent could not be read.")}
          <Button type="button" variant="outline" size="sm" onClick={() => report.refetch()}>
            {t_i18n("Retry")}
          </Button>
        </div>
      );
    }
    // Fewer than k people used the agent (design 12.5): the viewer's own share, or no hero at all.
    const underK = scopeUnderK(report.data);
    if (underK && !underK.viewerUsed) return <RoiUnderK kind="agent" {...underK} />;
    const state = fleetRoiState(report.data, lifetime.isError ? report.data : lifetime.data);
    if (state === null) return <RoiReportSkeleton />;
    if (state === "none") {
      // Only someone who can manage the agent picks its unit: a viewer is told who does (UX #9810 r3 B1).
      return canManage ? (
        <EmptyState
          icon={Target}
          title="This agent is not counted in ROI."
          description="Pick a unit of work in its configuration to count what it delivers."
          actionLabel="Pick a unit"
          actionIcon={Target}
          onAction={() => navigate(agentPickUnitPath(agentId))}
        />
      ) : (
        <EmptyState
          icon={Target}
          title="This agent is not counted in ROI."
          description="Ask the agent's owner to pick a unit of work: only someone who can manage the agent sets it."
        />
      );
    }
    if (state === "window") {
      const wider = widerFleetWindow(window);
      return (
        <EmptyState
          icon={Clock3}
          title="Nothing was counted in this period."
          actionLabel={wider ? "Widen the window" : undefined}
          actionIcon={Clock3}
          onAction={wider ? () => setWindow(wider) : undefined}
        />
      );
    }
    // One figure with the Value page, the list and the export (invariant d): the ledger's totals.
    const figures = scopeHeroFigures(report.data);
    return (
      <>
        {underK ? <RoiUnderK kind="agent" {...underK} /> : report.data.estimated && <RoiEstimatedNote />}
        <RoiKpiCards
          figures={figures}
          currency={report.data.currency}
          window={window}
          costMasked={report.data.cost_masked}
          onReview={underK ? undefined : () => navigate(agentReviewPath(agentId, window))}
        />
        {!underK && <RoiAssumptions facts={figures.assumption_facts ?? []} computation={figures.computation ?? []} />}
      </>
    );
  })();

  return (
    <section aria-labelledby="agent-roi-title" aria-busy={report.isLoading || undefined} className="space-y-3" data-testid="agent-roi-section">
      {header}
      {body}
      {/* The agent's chat deliverables, apart from its automation figures (#10544 S5). */}
      <RoiChatSeriesCard scope={{ kind: "agent", id: agentId }} window={window} urlKey="chat_" />
    </section>
  );
}
