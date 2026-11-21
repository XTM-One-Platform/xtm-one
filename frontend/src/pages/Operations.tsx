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
import { useLocation } from "react-router-dom";
import { useFormatter } from "@/i18n";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { ListPageHeader } from "@/components/ui/list-page-header";
import TaskHistory from "@/pages/TaskHistory";
import BackgroundTasks from "@/pages/BackgroundTasks";
import SharedStateTab from "@/pages/operations/SharedStateTab";
import WorksetsTab from "@/pages/operations/WorksetsTab";
import { OPERATIONS_TABS, operationsTabOf, operationsTabSearch } from "@/pages/operations/tabs";

/**
 * Operations - what is running or past: assignment-run executions,
 * background delegations, the shared cross-run state store and the worksets.
 * What to do and follow (work items, value, workspaces) is Tasks (#775).
 */
export default function Operations() {
  const { t_i18n } = useFormatter();
  const { pathname } = useLocation();
  const tab = operationsTabOf(pathname);

  return (
    <div className="space-y-6">
      {/* One header row (#10215, criterion 5): the page's help sentence is the title's "i". */}
      <ListPageHeader
        title={
          <span className="flex items-center gap-2">
            <Icon name="clipboard-list" size={24} className="text-accent" aria-hidden /> {t_i18n("Operations")}
          </span>
        }
        infoLabel={t_i18n("About operations")}
        info={t_i18n("What your agents are running and have run: executions, background delegations, the shared state they coordinate through, and the corpora they build to reason over more material than fits in a context.")}
      />

      <DetailTabs tabs={OPERATIONS_TABS} active={tab} searchFor={operationsTabSearch} />

      {tab === "executions" && <TaskHistory hideHeader />}
      {tab === "background-tasks" && <BackgroundTasks hideHeader />}
      {tab === "shared-state" && <SharedStateTab />}
      {tab === "worksets" && <WorksetsTab />}
    </div>
  );
}
