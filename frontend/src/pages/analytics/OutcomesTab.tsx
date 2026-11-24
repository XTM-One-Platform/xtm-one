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
 * The Outcomes tab of Analytics (#9706, design dev-docs/roi-outcomes.md 6.3,
 * "Platform-wide"): what the agents and fleets delivered over a window, in both
 * modes, for every agent or fleet that declares a unit of work. Administrators
 * only (D1 = B keeps the platform view theirs); the page hides the tab from
 * everyone else. Reading it never starts a run nor calls a model. The KPI
 * cards, columns and assumptions are shared with the fleet ROI view
 * (`components/roi/RoiReportParts`, #9785).
 */
import { valuePath } from "@/lib/task-routes";
import { useMemo } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Clock3, Target } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { RoiExportButtons } from "@/components/roi/RoiExportButtons";
import {
  RoiAssumptions,
  RoiBreakdown,
  RoiEstimatedNote,
  RoiKpiCards,
  RoiReportSkeleton,
} from "@/components/roi/RoiReportParts";
import { useRoiReport, type RoiWindow } from "@/hooks/api/useRoiReportApi";
import { useFormatter } from "@/i18n";
import { KIND_LABELS, ledgerOutcomeRows, outcomeRows, outcomesState, scopeHeroFigures, widerWindow } from "@/lib/roi-outcomes";

/** The windows of the tab (unchanged by #9836: the quarter is the Value page's). Translated by `SegmentedFilter`. @i18n-keys */
const WINDOW_LABELS = { "7d": "7D", "30d": "30D", "90d": "90D", all: "All" } as const satisfies Partial<Record<RoiWindow, string>>;
type TabWindow = keyof typeof WINDOW_LABELS;
const WINDOWS = Object.keys(WINDOW_LABELS) as TabWindow[];

function parseWindow(value: string | null): TabWindow {
  return value && value in WINDOW_LABELS ? (value as TabWindow) : "30d";
}

export function OutcomesTab() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const window = parseWindow(searchParams.get("window"));
  const setWindow = (next: TabWindow) =>
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous);
        if (next === "30d") params.delete("window");
        else params.set("window", next);
        return params;
      },
      { replace: true },
    );

  const report = useRoiReport(window);
  // The ledger's figures when the answer carries them, so the KPI cards and the rows read what the
  // Value page, the agent page and the export draw (#12077 slice L6b4, invariant d); the By agent
  // residual and folded rows are remainders, so the table holds what the hero holds (UX r1 B1).
  const table = useMemo(() => ledgerOutcomeRows(report.data) ?? { rows: outcomeRows(report.data), remainders: [] }, [report.data]);
  const currency = report.data?.currency ?? "USD";

  const windowSwitch = (
    <SegmentedFilter
      options={WINDOWS.map((key) => ({ key, label: WINDOW_LABELS[key] }))}
      value={window}
      onChange={setWindow}
      ariaLabel={t_i18n("Window")}
      selection="radio"
    />
  );

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      {windowSwitch}
      <RoiExportButtons window={window} />
    </div>
  );

  if (report.isLoading) {
    return (
      <div className="space-y-4" aria-busy="true">
        {header}
        <RoiReportSkeleton />
      </div>
    );
  }

  if (report.isError || !report.data) {
    return (
      <div className="space-y-4">
        {header}
        <div role="alert" className="flex flex-wrap items-center gap-3 content-compact text-feedback-error-primary">
          {t_i18n("The outcomes could not be read.")}
          <Button type="button" variant="outline" size="sm" onClick={() => report.refetch()}>
            {t_i18n("Retry")}
          </Button>
        </div>
      </div>
    );
  }

  const state = outcomesState(report.data, window);
  if (state !== "data") {
    const wider = widerWindow(window);
    return (
      <div className="space-y-4">
        {header}
        {state === "none" ? (
          <EmptyState
            icon={Target}
            title="No agent is measured yet."
            description="Pick a unit of work on an agent or a fleet."
            actionLabel="Open agents"
            actionIcon={Target}
            onAction={() => navigate("/agents")}
          />
        ) : (
          <EmptyState
            icon={Clock3}
            title="Nothing was counted in this period."
            actionLabel={wider ? "Widen the window" : undefined}
            actionIcon={Clock3}
            onAction={wider && wider !== "quarter" ? () => setWindow(wider) : undefined}
          />
        )}
      </div>
    );
  }

  const hero = scopeHeroFigures(report.data);
  return (
    <div className="space-y-4">
      {header}
      <p className="content-compact text-default-secondary">
        {t_i18n("Every user reads the value of the agents and fleets they can open on the Value page.")}{" "}
        <Link to={valuePath(undefined, window === "30d" ? { scope: "organization" } : { scope: "organization", window })} className="text-accent hover:underline">
          {t_i18n("Open Value")}
        </Link>
      </p>
      {report.data.estimated && <RoiEstimatedNote />}
      <RoiKpiCards figures={hero} currency={currency} window={window} costMasked={report.data.cost_masked} />
      <RoiBreakdown
        rows={table.rows}
        remainders={table.remainders}
        currency={currency}
        withKind
        urlKey="oc_"
        storageKey="analytics-outcomes"
        searchPlaceholder={t_i18n("Search fleets or agents...")}
        filters={[
          {
            key: "kind",
            label: "Type",
            values: (row) => [row.kind],
            options: [
              { value: "fleet", label: t_i18n(KIND_LABELS.fleet) },
              { value: "agent", label: t_i18n(KIND_LABELS.agent) },
            ],
          },
        ]}
      />
      <RoiAssumptions facts={hero.assumption_facts ?? []} computation={hero.computation ?? []} />
    </div>
  );
}
