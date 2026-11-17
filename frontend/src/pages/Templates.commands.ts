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
 * Document and deck templates.
 */
import { LayoutTemplate, Plus } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "templates.create",
    category: "Templates",
    title: "Create a template",
    icon: Plus,
    requires: "agenticAI",
    navigate: "/templates/new",
  },
  {
    id: "templates.list",
    category: "Templates",
    title: "Browse templates",
    icon: LayoutTemplate,
    navigate: "/templates",
  },
]);
