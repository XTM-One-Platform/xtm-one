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
 * Prompts.
 */
import { FileText, Plus } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "prompts.create",
    category: "Prompts",
    title: "Create a prompt",
    icon: Plus,
    requires: "agenticAI",
    navigate: "/prompts/new",
  },
  {
    id: "prompts.list",
    category: "Prompts",
    title: "Browse prompts",
    icon: FileText,
    navigate: "/prompts",
  },
]);
