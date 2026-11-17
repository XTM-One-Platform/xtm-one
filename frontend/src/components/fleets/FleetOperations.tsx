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
 * A fleet's Operations tab: the Operations page's lists locked on the
 * fleet. Each list shows what the fleet's current home members produced or own
 * (orchestrator included, connected agents left out), resolved server-side by
 * `?fleet=` (`fleet_views.fleet_scope_clause`), so the counts are the lists'
 * own. Same components, same filters, pagination and details as the
 * Operations page; the list shown is the path segment
 * (`/fleets/:fleetId/operations/:list`, #10203 S2; the lists own their query).
 * The list is picked with a labelled "List" select, a control of the tab and
 * never a second row of tabs under the fleet tabs (#9980, UX round 1 B2). The
 * select leads the list's own filter row (`toolbarLead`) and the tab's intro
 * sentence sits in the "i" beside it, so the tab opens on one control row
 * (header density sweep, #10215 slice b: no help paragraph, no second row).
 */
import { Activity, Database, Library, ListChecks, PlayCircle, type LucideIcon } from "lucide-react";
import { Navigate, useLocation, useNavigate, useParams } from "react-router-dom";

import { InfoTip } from "@/components/ui/info-tip";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useFormatter } from "@/i18n";
import { fleetViewPath, readFleetOpsList, type FleetOpsList } from "@/lib/fleet-operations";
import BackgroundTasks from "@/pages/BackgroundTasks";
import TaskHistory from "@/pages/TaskHistory";
import SharedStateTab from "@/pages/operations/SharedStateTab";
import WorkItemsTab from "@/pages/operations/WorkItemsTab";
import WorksetsTab from "@/pages/operations/WorksetsTab";

const LISTS: { key: FleetOpsList; label: string; icon: LucideIcon }[] = [
  { key: "runs", label: "Runs", icon: PlayCircle },
  { key: "background-tasks", label: "Background Tasks", icon: Activity },
  { key: "work-items", label: "Work Items", icon: ListChecks },
  { key: "state", label: "Shared State", icon: Database },
  { key: "worksets", label: "Worksets", icon: Library },
];

export function FleetOperations({ fleetId, fleetName }: { fleetId: string; fleetName: string }) {
  const { t_i18n } = useFormatter();
  const { list: listSegment } = useParams<{ list?: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const list = readFleetOpsList(listSegment ?? null);
  // Another list is its own path and starts clean: the filters of one never narrow the next.
  const select = (next: FleetOpsList) => navigate(fleetViewPath(fleetId, "operations", next), { state: location.state });
  const empty = t_i18n("Nothing yet. What the agents of “{name}” create appears here.", { name: fleetName });

  // `/operations` alone or an unknown list segment shows Runs: the address says so too (UX A1 on #10220).
  if (listSegment !== list) {
    return <Navigate to={`${fleetViewPath(fleetId, "operations", list)}${location.search}`} replace state={location.state} />;
  }

  const lead = (
    <div className="flex shrink-0 items-center gap-2" data-testid="fleet-operations-lead">
      <InfoTip
        label={t_i18n("About this tab")}
        content={t_i18n("What the agents of “{name}” run, delegate and produce, as they do it.", { name: fleetName })}
      />
      <Select value={list} onValueChange={(next) => select(next as FleetOpsList)}>
        {/* 32 px toolbar height (TOOLBAR_CONTROL_HEIGHT) and type, like the search and segments beside it (UX round 1 B2 on #10260).
            224 px wide, one line: the longest label of the 4 locales (es "Tareas en segundo plano", 165 px) fits uncut (UX round 3 B1). */}
        <SelectTrigger className="h-8 w-56 whitespace-nowrap content-compact" aria-label={t_i18n("Operations list")} data-testid="fleet-operations-list">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {LISTS.map(({ key, label, icon: Icon }) => (
            <SelectItem key={key} value={key}>
              <span className="flex items-center gap-2">
                <Icon className="size-4" aria-hidden="true" />
                {t_i18n(label)}
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );

  return (
    <div data-testid="fleet-operations">
      {list === "runs" && <TaskHistory hideHeader fleet={fleetId} toolbarLead={lead} />}
      {list === "background-tasks" && <BackgroundTasks hideHeader fleet={fleetId} toolbarLead={lead} />}
      {list === "work-items" && <WorkItemsTab fleet={fleetId} emptyDescription={empty} toolbarLead={lead} />}
      {list === "state" && <SharedStateTab fleet={fleetId} emptyDescription={empty} toolbarLead={lead} />}
      {list === "worksets" && <WorksetsTab fleet={fleetId} toolbarLead={lead} />}
    </div>
  );
}
