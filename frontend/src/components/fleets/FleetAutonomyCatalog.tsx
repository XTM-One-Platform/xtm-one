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

import { Icon } from "@filigran/design-system";
/**
 * The fleet's approval gates as a level matrix (#9769 S4, design round 2 A2):
 * one row per declared gate (the backend's `autonomy_catalog`, the same data
 * the run-time gate and the autonomy strip read), one column per autonomy
 * level, each cell saying whether the action waits for a person or runs on
 * its own. The fleet's current level is the highlighted column. The matrix is
 * the content itself (a level grid, not a list of records), so it is a plain
 * table on the data-table CONTENT list.
 *
 * The cells read "Waits" or "Runs alone", the words of the strip's lists
 * (UX round 1 B3 on #10034); `source` is the one line under the matrix that
 * says where the catalog comes from (C3).
 */

import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import type { FleetAutonomyGate, FleetAutonomyLevel } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { AUTONOMY_LEVEL_LABELS, AUTONOMY_LEVELS, gateActionLabel, IMPACT_LABELS } from "./fleet-autonomy";
import { FleetGateMark } from "./FleetModuleMark";

export function FleetAutonomyCatalog({
  catalog,
  level,
  source,
}: {
  catalog: FleetAutonomyGate[];
  level: FleetAutonomyLevel;
  /** Where the catalog comes from, one translated line under the matrix. */
  source?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Card data-testid="about-autonomy-catalog">
      <CardContent className="space-y-3 p-4">
        <p className="content-compact text-default-secondary">
          {t_i18n("What waits for a person at each level of human approval. The highlighted column is this fleet's level.")}
        </p>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] border-collapse content-compact">
            <caption className="sr-only">{t_i18n("Approval gates by level of human approval")}</caption>
            <thead>
              <tr className="border-b border-[var(--border-elevation-subtle-soft)]">
                <th scope="col" className="py-2 pr-3 text-left font-medium text-default-secondary">
                  {t_i18n("Action")}
                </th>
                {AUTONOMY_LEVELS.map((key) => (
                  <th
                    key={key}
                    scope="col"
                    aria-current={key === level ? "true" : undefined}
                    className={cn(
                      "px-2 py-2 text-left font-medium whitespace-nowrap",
                      // The current column: an accent top rule (3:1 or more) on top of the tint (UX #9945 A2).
                      key === level
                        ? "border-t-2 border-t-accent bg-elevation-highlight text-default-primary"
                        : "text-default-secondary",
                    )}
                    data-testid={`about-autonomy-col-${key}`}
                  >
                    {t_i18n(AUTONOMY_LEVEL_LABELS[key])}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {catalog.map((gate) => (
                <tr key={gate.key} className="border-b border-[var(--border-elevation-subtle-soft)] last:border-0">
                  <th scope="row" className="max-w-[320px] py-2 pr-3 text-left font-normal">
                    <span className="flex items-center gap-2">
                      <FleetGateMark gate={gate} />
                      <span className="min-w-0">
                        <span className="block font-semibold">{gateActionLabel(gate, t_i18n)}</span>
                        <span className="block text-default-secondary">{t_i18n(IMPACT_LABELS[gate.impact])}</span>
                      </span>
                    </span>
                  </th>
                  {AUTONOMY_LEVELS.map((key) => {
                    const waits = gate.by_level?.[key] ?? true;
                    return (
                      <td key={key} className={cn("px-2 py-2 align-middle", key === level && "bg-elevation-highlight")}>
                        {waits ? (
                          <Badge variant="warning" className="gap-1 whitespace-nowrap">
                            <Icon name="shield-check" size={16} />
                            {t_i18n("Waits")}
                          </Badge>
                        ) : (
                          <span className="whitespace-nowrap text-default-secondary">{t_i18n("Runs alone")}</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {source && (
          <p className="content-compact text-default-secondary" data-testid="about-autonomy-source">
            {source}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
