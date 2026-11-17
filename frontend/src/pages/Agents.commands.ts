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
 * Agents list — creation, import, and the handover graph.
 */
import { Bot, Upload, Wand2, Workflow } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "agents.create",
    category: "Agents",
    title: "Create an agent",
    keywords: ["new", "template", "persona"],
    icon: Wand2,
    requires: "agenticAI",
    navigate: "/agents/new",
  },
  {
    id: "agents.import",
    category: "Agents",
    title: "Import an agent from a file",
    keywords: ["bundle", "agentschema", "yaml", "json"],
    icon: Upload,
    requires: "agenticAI",
    navigate: "/agents/new/import",
  },
  {
    id: "agents.flow",
    category: "Agents",
    title: "Open the handover graph",
    keywords: ["topology", "delegation"],
    icon: Workflow,
    navigate: "/agents/flow",
  },
  {
    id: "agents.list",
    category: "Agents",
    title: "Browse agents",
    icon: Bot,
    navigate: "/agents",
  },
]);
