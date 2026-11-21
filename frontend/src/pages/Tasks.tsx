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
import { useLocation, useParams } from "react-router-dom";
import { useFormatter } from "@/i18n";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { ListPageHeader } from "@/components/ui/list-page-header";
import WorkItemsTab from "@/pages/operations/WorkItemsTab";
import WorkspacesTab, { NewWorkspaceButton } from "@/pages/operations/WorkspacesTab";
import Value from "@/pages/Value";
import RouteNotFound from "@/pages/RouteNotFound";
import { isValueTab } from "@/lib/roi-value";
import { TASKS_TABS, tasksTabOf, tasksTabSearch } from "@/pages/operations/tabs";

/**
 * Tasks (#775) - what to do and follow: the work items of agents and people
 * (the default tab), the value of that work, and the workspaces it is filed
 * in. What is running or past stays in Operations. The tabs render the same
 * components Operations rendered before, moved, not rewritten; the Value tab
 * keeps its own header (its scope and window controls sit in it).
 */
export default function Tasks() {
  const { t_i18n } = useFormatter();
  const { pathname } = useLocation();
  const tab = tasksTabOf(pathname);
  const { tab: valueSegment } = useParams<{ tab?: string }>();

  // An unknown Value table (`/tasks/value/by-agent`) is not found before the
  // Tasks header and tab row render: one full-page rendering, no redirect (#797).
  if (tab === "value" && !isValueTab(valueSegment)) return <RouteNotFound />;

  return (
    <div className="space-y-6">
      {/* One header row (#10215, criterion 5): the page's help sentence is the title's "i". */}
      <ListPageHeader
        title={
          <span className="flex items-center gap-2">
            <Icon name="list-checks" size={24} className="text-accent" aria-hidden /> {t_i18n("Tasks page")}
          </span>
        }
        infoLabel={t_i18n("About tasks")}
        info={t_i18n("What to do and follow: work items, value, workspaces.")}
        // The primary action of the open tab stays in the page header, never in its table toolbar.
        actions={tab === "workspaces" ? <NewWorkspaceButton /> : undefined}
      />

      <DetailTabs tabs={TASKS_TABS} active={tab} searchFor={tasksTabSearch} testId="tasks-tabs" />

      {tab === "work-items" && <WorkItemsTab itemInPath />}
      {tab === "value" && <Value embedded />}
      {tab === "workspaces" && <WorkspacesTab />}
    </div>
  );
}
