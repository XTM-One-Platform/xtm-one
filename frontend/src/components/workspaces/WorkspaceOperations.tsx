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
 * A workspace's Operations tab: the Operations page's work item,
 * shared state and workset lists, locked on the workspace. Each list shows
 * what is filed in the workspace and what the fleet it hosts produced (owned,
 * or for a work item created, by a current home member), resolved server-side
 * by `?workspace=` (`fleet_views.workspace_scope_clause`), so the counts are
 * the lists' own. Same components, same filters and pagination as the
 * Operations page; the list shown is held in `?list=`.
 */
import { Database, Library, ListChecks } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import { SegmentedFilter, type SegmentedFilterOption } from "@/components/ui/segmented-filter";
import { useFormatter } from "@/i18n";
import { readWorkspaceOpsList, type WorkspaceOpsList } from "@/lib/workspace-tabs";
import SharedStateTab from "@/pages/operations/SharedStateTab";
import WorkItemsTab from "@/pages/operations/WorkItemsTab";
import WorksetsTab from "@/pages/operations/WorksetsTab";

const LISTS: SegmentedFilterOption<WorkspaceOpsList>[] = [
  { key: "work-items", label: "Work Items", icon: ListChecks },
  { key: "state", label: "Shared State", icon: Database },
  { key: "worksets", label: "Worksets", icon: Library },
];

export function WorkspaceOperations({ workspaceId, fleetName }: { workspaceId: string; fleetName?: string }) {
  const { t_i18n } = useFormatter();
  const [searchParams, setSearchParams] = useSearchParams();
  const list = readWorkspaceOpsList(searchParams.get("list"));
  // Another list starts clean: the filters of one never narrow the next.
  const select = (next: WorkspaceOpsList) =>
    setSearchParams(next === "work-items" ? {} : { list: next }, { replace: true });
  // A hosted fleet's empty list says where its work will appear,
  // not how to enable a tool on an agent.
  const fleetEmpty = fleetName
    ? t_i18n(
        "Nothing yet. What the agents of “{name}” create appears here, even when they file it nowhere.",
        { name: fleetName },
      )
    : undefined;

  return (
    <div className="space-y-4">
      <p className="content-compact text-default-secondary">
        {fleetName
          ? t_i18n(
              "What is filed in this workspace, and what the agents of “{name}” produce, as they produce it.",
              { name: fleetName },
            )
          : t_i18n("What is filed in this workspace.")}
      </p>
      <SegmentedFilter
        options={LISTS}
        value={list}
        onChange={select}
        selection="radio"
        ariaLabel={t_i18n("Operations list")}
        testId="workspace-operations-list"
      />
      {list === "work-items" && <WorkItemsTab workspace={workspaceId} emptyDescription={fleetEmpty} />}
      {list === "state" && <SharedStateTab workspace={workspaceId} emptyDescription={fleetEmpty} />}
      {list === "worksets" && <WorksetsTab workspace={workspaceId} />}
    </div>
  );
}
