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
 * What each gated action is and whether it waits at the level picked (#1373):
 * the setup step's structured table under the level selector, in place of the
 * levels' tooltip. One row per gated action: its integration or module mark,
 * the action, what it writes and why it matters, and its state at the level,
 * updated live as the level changes. Same catalog as the strip and the matrix.
 */
import { Icon } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import type { FleetAutonomyGate, FleetAutonomyLevel } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { AUTONOMY_LEVEL_LABELS, IMPACT_LABELS, ctemGateEffect, gateActionLabel } from "./fleet-autonomy";
import { GATE_IMPACT_EXPLANATIONS, GATE_STATE_LABELS, gateRows } from "./fleet-gates-table";
import { FleetGateMark } from "./FleetModuleMark";

export function FleetGatesAtLevel({ catalog, level }: { catalog: FleetAutonomyGate[]; level: FleetAutonomyLevel }) {
  const { t_i18n } = useFormatter();
  if (catalog.length === 0) return null;
  return (
    <div className="overflow-x-auto rounded-sm border border-[var(--border-elevation-subtle-soft)]" data-testid="fleet-gates-table">
      <table className="w-full table-fixed content-compact">
        <caption className="sr-only">
          {t_i18n("Gated actions at {level}", { level: t_i18n(AUTONOMY_LEVEL_LABELS[level]) })}
        </caption>
        <colgroup>
          <col className="w-[34%]" />
          <col />
          <col className="w-[200px]" />
        </colgroup>
        <thead>
          <tr className="border-b border-[var(--border-elevation-subtle-soft)] text-left text-default-secondary">
            <th scope="col" className="px-3 py-2 font-medium">
              {t_i18n("Action")}
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              {t_i18n("What is gated")}
            </th>
            <th scope="col" className="px-3 py-2 font-medium" aria-current="true">
              {t_i18n("At {level}", { level: t_i18n(AUTONOMY_LEVEL_LABELS[level]) })}
            </th>
          </tr>
        </thead>
        <tbody>
          {gateRows(catalog, level).map(({ gate, state }) => {
            const effect = ctemGateEffect(gate);
            return (
              <tr
                key={gate.key}
                className="border-b border-[var(--border-elevation-subtle-soft)] last:border-0 align-top"
                data-testid="fleet-gates-row"
                data-state={state}
              >
                <th scope="row" className="px-3 py-2 text-left font-normal">
                  <span className="flex min-w-0 items-start gap-2">
                    <FleetGateMark gate={gate} className="mt-0.5 h-4 w-4" />
                    <span className="min-w-0 font-medium text-default-primary">{gateActionLabel(gate, t_i18n)}</span>
                  </span>
                </th>
                <td className="px-3 py-2 text-default-secondary">
                  <span className="block font-medium text-default-primary">{t_i18n(IMPACT_LABELS[gate.impact])}</span>
                  <span className="block" data-testid="fleet-gates-explanation">
                    {effect ? t_i18n(effect) : t_i18n(GATE_IMPACT_EXPLANATIONS[gate.impact])}
                  </span>
                </td>
                <td className="px-3 py-2">
                  {state === "waits" ? (
                    <Badge variant="warning" className="gap-1 whitespace-nowrap">
                      <Icon name="shield-check" size={16} aria-hidden />
                      {t_i18n(GATE_STATE_LABELS.waits)}
                    </Badge>
                  ) : (
                    <Badge variant="secondary" className="gap-1 whitespace-nowrap">
                      <Icon name="zap" size={16} aria-hidden />
                      {t_i18n(GATE_STATE_LABELS.runs)}
                    </Badge>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
