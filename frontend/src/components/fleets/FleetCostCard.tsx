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
 * The fleet's cost: what the runs attributed to
 * the fleet spent over a window, in total and by agent
 * (`GET /fleets/{id}/cost?days=`). A connected member's runs count for
 * its own home fleet, never for this one. Managers only: the API refuses
 * anyone else, so the page does not mount the card for them. The window is
 * the page's shared `?window=` (#9785), the one the ROI view reads, so the
 * two never show different periods.
 */
import { Coins } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Skeleton } from "@/components/ui/skeleton";
import { useFleetCost } from "@/hooks/api/useFleetApi";
import { useFleetWindow } from "@/hooks/useFleetWindow";
import { useFormatter } from "@/i18n";
import { FLEET_WINDOW_LABELS, FLEET_WINDOWS, fleetWindowDays } from "@/lib/fleet-roi-window";
import { exactCost, formatCost } from "@/lib/trace-utils";

import { costRows } from "./fleet-cost";

export function FleetCostCard({ fleetId }: { fleetId: string }) {
  const { t_i18n, intl } = useFormatter();
  const [window, setWindow] = useFleetWindow();
  const { data, isLoading, error } = useFleetCost(fleetId, fleetWindowDays(window));
  const rows = costRows(data);

  return (
    <Card>
      <CardHeader className="space-y-2 pb-2">
        <CardTitle className="flex items-center gap-2 content-base">
          <Coins className="h-4 w-4 text-accent" aria-hidden /> {t_i18n("Fleet cost")}
        </CardTitle>
        <SegmentedFilter
          options={FLEET_WINDOWS.map((key) => ({ key, label: FLEET_WINDOW_LABELS[key] }))}
          value={window}
          onChange={setWindow}
          size="sm"
          selection="radio"
          ariaLabel={t_i18n("Cost window")}
          className="w-fit"
        />
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading ? (
          <Skeleton className="h-16 w-full" />
        ) : error || !data ? (
          <p role="alert" className="content-caption text-default-secondary">
            {t_i18n("The cost of this fleet could not be read.")}
          </p>
        ) : (
          <>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-2xl font-semibold font-mono-plex" title={exactCost(data.total_cost, intl.locale)}>
                {formatCost(data.total_cost, intl.locale)}
              </span>
              <span className="content-caption text-default-secondary">
                {t_i18n("{count, plural, one {# run} other {# runs}}", { count: data.trace_count })}
              </span>
            </div>
            {rows.length === 0 ? (
              <p className="content-caption text-default-secondary">
                {t_i18n("No run of a home member in this window.")}
              </p>
            ) : (
              <ul aria-label={t_i18n("Cost by agent")} className="space-y-1.5">
                {rows.map((row) => (
                  <li key={row.key} className="space-y-1">
                    <div className="flex items-center justify-between gap-2 content-compact">
                      <span className="min-w-0 truncate" title={row.name}>
                        {row.name || t_i18n("Unknown agent")}
                      </span>
                      <span className="shrink-0 font-mono-plex" title={exactCost(row.cost, intl.locale)}>
                        {formatCost(row.cost, intl.locale)}
                      </span>
                    </div>
                    <div className="h-1 rounded-full bg-elevation-hover" aria-hidden>
                      <div className="h-1 rounded-full bg-accent" style={{ width: `${row.share}%` }} />
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
