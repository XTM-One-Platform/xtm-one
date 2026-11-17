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
 * Operations. One parent with the four tabs as sub-entries.
 */
import { ClipboardList } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "operations.open",
    category: "Operations",
    title: "Open operations",
    keywords: ["runs", "tasks", "queue"],
    icon: ClipboardList,
    children: [
      { id: "operations.executions", category: "Operations", title: "Executions", navigate: "/operations/executions" },
      { id: "operations.backgroundTasks", category: "Operations", title: "Background Tasks", navigate: "/operations/background-tasks" },
      { id: "operations.workItems", category: "Operations", title: "Work Items", navigate: "/operations/work-items" },
      { id: "operations.sharedState", category: "Operations", title: "Shared State", navigate: "/operations/state" },
    ],
  },
]);
