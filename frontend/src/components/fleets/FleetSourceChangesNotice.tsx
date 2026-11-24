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
 * On a forked fleet's page: "the fleet you forked has changed since", with the
 * structural diff behind a disclosure (`GET /fleets/{id}/source-changes`, #9174).
 * Read only: it names members and fields, never a persona; nothing is applied
 * to the copy, which stays the reader's own. Hidden when the fleet is not a
 * fork, its source is gone, nothing changed or the read failed. Members and
 * connected agents or fleets read by their display names, from the agents and
 * fleets the page already loads, never by an id.
 */
import { GitFork } from "lucide-react";
import { useMemo } from "react";

import { DashboardNotice, NoticeAction } from "@/components/ui/dashboard-notice";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useFleetSourceChanges, useFleets } from "@/hooks/api/useFleetApi";
import { useFormatter } from "@/i18n";
import { formatList } from "@/lib/format-list";

import {
  changedFieldLabels,
  currentSourceName,
  describeSourceChange,
  showSourceChanges,
  sourceChanged,
  sourceChangeNames,
  sourceSuccessor,
} from "./fleet-source-changes";

export function FleetSourceChangesNotice({
  fleetId,
  source,
}: {
  fleetId: string;
  source: { name: string; isBuiltin: boolean; sourceId: string };
}) {
  const { t_i18n, locale } = useFormatter();
  const { data } = useFleetSourceChanges(fleetId);
  // The page's agents list and the shared optional fleets list (a failure there
  // only leaves a connected fleet unnamed, never the page's ErrorBoundary).
  const { data: agents } = useAgents({ scope: "all" });
  const { data: fleets } = useFleets({ optional: true });
  const names = useMemo(() => sourceChangeNames(agents, fleets), [agents, fleets]);
  if (!showSourceChanges(data)) return null;

  const name = currentSourceName(data, source.name);
  // CTEM-31: a built-in source folded into another seeded fleet names that fleet.
  const successor = sourceSuccessor(data);
  const changed = sourceChanged(data);
  const title = successor
    ? t_i18n("The built-in fleet {name} is now part of {successor}", { name: source.name, successor: successor.name })
    : source.isBuiltin
      ? t_i18n("The built-in fleet {name} has changed since you duplicated it", { name })
      : t_i18n("The fleet {name} has changed since you duplicated it", { name });

  return (
    <DashboardNotice
      variant="info"
      icon={GitFork}
      title={title}
      actions={
        successor ? (
          <NoticeAction variant="info" to={`/fleets/${successor.fleet_id}`}>
            {t_i18n("Open {name}", { name: successor.name })}
          </NoticeAction>
        ) : (
          <NoticeAction variant="info" to={`/fleets/${source.sourceId}`}>
            {t_i18n("Open the original fleet")}
          </NoticeAction>
        )
      }
    >
      <p>
        {successor
          ? t_i18n("Your copy is not changed and keeps running. New built-in improvements arrive in {successor}.", { successor: successor.name })
          : t_i18n("Your copy is not changed. Apply what you want from the original by hand.")}
      </p>
      {!changed ? null : data.diff_available ? (
        <details className="mt-1.5" data-testid="fleet-source-changes">
          <DisclosureSummary>
            {t_i18n("{count, plural, one {# change} other {# changes}}", { count: data.changes.length })}
          </DisclosureSummary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {data.changes.map((change) => {
              const fields = changedFieldLabels(change, t_i18n);
              const line = describeSourceChange(change, t_i18n, names);
              return (
                <li key={`${change.object}:${change.name}:${change.action}`}>
                  {fields.length > 0 ? t_i18n("{change}: {fields}", { change: line, fields: formatList(fields, locale) }) : line}
                </li>
              );
            })}
          </ul>
        </details>
      ) : (
        <p className="mt-1">{t_i18n("This copy was made before changes were recorded, so the detail is not available.")}</p>
      )}
    </DashboardNotice>
  );
}
