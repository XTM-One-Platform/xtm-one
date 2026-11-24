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
 * A list of gates, one per line, each with its mark and its full action name
 * (UX round 1 B2 and B3 on #1840). The name wraps and is never cut: the list is
 * what a confirmation or a choice exists to show, and a gate label can carry a
 * "; " of its own, so the names are never joined on one line. Shared by the
 * level strip's confirmation, the CTEM setup's gate choice and its confirmation.
 */
import type { FleetApprovalGate, FleetDocModule } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";

import { gateActionLabel } from "./fleet-autonomy";
import { FleetGateMark } from "./FleetModuleMark";

type ListedGate = Pick<FleetApprovalGate, "key" | "action" | "impact" | "provider"> & { module: FleetDocModule };

/**
 * `phrasing`: the same lines drawn with spans, for a list inside a button (an
 * option card's description), where a `ul` is not allowed.
 */
export function FleetGateList({ gates, testId, phrasing = false }: { gates: ListedGate[]; testId?: string; phrasing?: boolean }) {
  const { t_i18n } = useFormatter();
  const List = phrasing ? "span" : "ul";
  const Item = phrasing ? "span" : "li";
  return (
    <List className="block space-y-1.5" data-testid={testId}>
      {gates.map((gate) => (
        <Item key={gate.key} className="flex items-start gap-2 content-compact" data-testid="fleet-gate-list-item">
          <FleetGateMark gate={gate} className="mt-0.5 h-4 w-4" />
          <span className="min-w-0 break-words">{gateActionLabel(gate, t_i18n)}</span>
        </Item>
      ))}
    </List>
  );
}
