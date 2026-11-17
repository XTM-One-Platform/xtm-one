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

import { ClipboardList } from "lucide-react";
import { useFormatter } from "@/i18n";
import { useTabParam } from "@/hooks/useTabParam";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { ListPageHeader } from "@/components/ui/list-page-header";
import TaskHistory from "@/pages/TaskHistory";
import BackgroundTasks from "@/pages/BackgroundTasks";
import WorkItemsTab from "@/pages/operations/WorkItemsTab";
import SharedStateTab from "@/pages/operations/SharedStateTab";
import WorksetsTab from "@/pages/operations/WorksetsTab";
import WorkspacesTab, { NewWorkspaceButton } from "@/pages/operations/WorkspacesTab";
import { OPERATIONS_TABS, operationsTabSearch, type OpsTab } from "@/pages/operations/tabs";

/**
 * Operations - the single hub for everything that is the underlying work of
 * agents: assignment-run executions, background delegations, durable work
 * items, and the shared cross-run state store. Consolidates what used to be
 * separate History / Background Tasks / Work Items entries.
 */
export default function Operations() {
  const { t_i18n } = useFormatter();
  const [tab] = useTabParam<OpsTab>("executions");

  return (
    <div className="space-y-6">
      {/* One header row (#10215, criterion 5): the page's help sentence is the title's "i". */}
      <ListPageHeader
        title={
          <span className="flex items-center gap-2">
            <ClipboardList className="h-7 w-7 text-accent" /> {t_i18n("Operations")}
          </span>
        }
        infoLabel={t_i18n("About operations")}
        info={t_i18n("Everything your agents are working on - run executions, background delegations, durable work items, the shared state they coordinate through, and the corpora they build to reason over more material than fits in a context.")}
        // The primary action of the open tab stays in the page header, never in its table toolbar.
        actions={tab === "workspaces" ? <NewWorkspaceButton /> : undefined}
      />

      <DetailTabs tabs={OPERATIONS_TABS} active={tab} searchFor={operationsTabSearch} />

      {tab === "executions" && <TaskHistory hideHeader />}
      {tab === "background-tasks" && <BackgroundTasks hideHeader />}
      {tab === "work-items" && <WorkItemsTab />}
      {tab === "state" && <SharedStateTab />}
      {tab === "worksets" && <WorksetsTab />}
      {tab === "workspaces" && <WorkspacesTab />}
    </div>
  );
}
