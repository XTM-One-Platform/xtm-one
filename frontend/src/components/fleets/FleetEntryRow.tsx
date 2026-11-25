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
 * Where people enter the fleets, and the agents in none (#9977 S2, mockups r3
 * APPROVE under f88d65c6, B5 and B6), around the Fleets list grid:
 *
 * - `FleetEntryRow`, one line above the grid, "People enter from" then one chip
 *   per entry point (an agent that hands over to a fleet's exposed agent).
 *   A chip opens the agent; its hover and keyboard focus name the fleets it
 *   reaches. The chips that fit, then "+N" whose hover and focus list the rest.
 *   No entry point: one muted sentence, no chip.
 * - `NotInAFleetLine`, one muted line in the toolbar row, right-aligned before the count, "Not in a fleet: N
 *   agents", whose link opens the agents list filtered on them
 *   (`/agents?fleet=none`, the filter in the URL).
 */
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import type { FleetEntryPoint } from "@/lib/fleet-card-facts";

/** The chips one line holds at 1024 before "+N". */
const CHIPS_SHOWN = 3;

function ReachTip({ point, fleetNames }: { point: FleetEntryPoint; fleetNames: ReadonlyMap<string, string> }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-1">
      <p className="font-medium">{t_i18n("{name} hands over to", { name: point.name })}</p>
      <ul className="space-y-0.5">
        {point.fleetIds.map((id) => (
          <li key={id}>{fleetNames.get(id) ?? id}</li>
        ))}
      </ul>
    </div>
  );
}

export function FleetEntryRow({
  entryPoints,
  fleetNames,
}: {
  entryPoints: readonly FleetEntryPoint[];
  fleetNames: ReadonlyMap<string, string>;
}) {
  const { t_i18n } = useFormatter();
  const shown = entryPoints.slice(0, entryPoints.length > CHIPS_SHOWN ? CHIPS_SHOWN : entryPoints.length);
  const hidden = entryPoints.slice(shown.length);
  return (
    <div className="flex min-w-0 items-center gap-2" data-testid="fleet-entry-row">
      <span className="shrink-0 content-compact font-medium text-default-secondary">{t_i18n("People enter from")}</span>
      {entryPoints.length === 0 ? (
        <span className="content-compact text-default-secondary">{t_i18n("No agent hands over to a fleet yet.")}</span>
      ) : (
        <>
          {shown.map((point) => (
            <Tip key={point.agentId} content={<ReachTip point={point} fleetNames={fleetNames} />}>
              <Link
                to={`/agents/${point.agentId}`}
                className="flex min-w-0 shrink items-center gap-1.5 rounded-sm border border-elevation-subtle px-2 py-0.5 content-compact hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                data-testid="fleet-entry-chip"
              >
                <Icon name="bot" size={16} className="shrink-0" />
                <span className="truncate">{point.name}</span>
                <span className="shrink-0 text-default-secondary">
                  {t_i18n("· {count, plural, one {# fleet} other {# fleets}}", { count: point.fleetIds.length })}
                </span>
              </Link>
            </Tip>
          ))}
          {hidden.length > 0 && (
            <Tip
              content={
                <ul className="space-y-0.5">
                  {hidden.map((point) => (
                    <li key={point.agentId}>{point.name}</li>
                  ))}
                </ul>
              }
            >
              <button
                type="button"
                className="shrink-0 rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                aria-label={t_i18n("{count, plural, one {# more entry point} other {# more entry points}}", { count: hidden.length })}
              >
                <Badge variant="outline" className="tabular-nums">
                  {`+${hidden.length}`}
                </Badge>
              </button>
            </Tip>
          )}
        </>
      )}
    </div>
  );
}

export function NotInAFleetLine({ count }: { count: number }) {
  const { t_i18n } = useFormatter();
  if (count === 0) return null;
  return (
    <span className="whitespace-nowrap content-compact text-default-secondary" data-testid="fleet-not-in-a-fleet">
      {t_i18n("Not in a fleet: {count, plural, one {# agent} other {# agents}}", { count })}{" "}
      <Link to="/agents?fleet=none" className="font-medium text-accent hover:underline">
        {t_i18n("Show in Agents")}
      </Link>
    </span>
  );
}
