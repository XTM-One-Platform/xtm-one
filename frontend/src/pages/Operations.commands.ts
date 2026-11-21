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
 * Operations and Tasks (#775). One parent each, its tabs as sub-entries.
 * The targets are literals (validate:commands reads them statically); they
 * are the URIs lib/task-routes.ts builds, pinned by task-routes.test.ts.
 */
import { ClipboardList, ListChecks } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "operations.open",
    category: "Operations",
    title: "Open operations",
    keywords: ["runs", "queue", "history"],
    icon: ClipboardList,
    children: [
      { id: "operations.executions", category: "Operations", title: "Executions", navigate: "/operations/executions" },
      { id: "operations.backgroundTasks", category: "Operations", title: "Background Tasks", navigate: "/operations/background-tasks" },
      { id: "operations.sharedState", category: "Operations", title: "Shared State", navigate: "/operations/shared-state" },
      { id: "operations.worksets", category: "Operations", title: "Worksets", navigate: "/operations/worksets" },
    ],
  },
  {
    id: "tasks.open",
    category: "Tasks",
    title: "Open tasks",
    keywords: ["work items", "to do", "value", "workspaces"],
    icon: ListChecks,
    children: [
      { id: "tasks.workItems", category: "Tasks", title: "Work Items", navigate: "/tasks/work-items" },
      { id: "tasks.value", category: "Tasks", title: "Value", navigate: "/tasks/value" },
      { id: "tasks.workspaces", category: "Tasks", title: "Workspaces", navigate: "/tasks/workspaces" },
    ],
  },
]);
