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
 * The three hub pages — one per nav group that owns a destination.
 */
import { Briefcase, Puzzle, Sparkles } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "hubs.work",
    category: "Navigation",
    title: "Open Work",
    icon: Briefcase,
    navigate: "/work",
  },
  {
    id: "hubs.agentStudio",
    category: "Navigation",
    title: "Open Agent Studio",
    icon: Sparkles,
    navigate: "/agent-studio",
  },
  {
    id: "hubs.toolsAndIntegrations",
    category: "Navigation",
    title: "Open Tools & Integrations",
    icon: Puzzle,
    navigate: "/tools-and-integrations",
  },
]);
