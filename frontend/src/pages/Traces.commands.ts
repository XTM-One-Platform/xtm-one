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
 * Observability surfaces: traces, sessions, analytics.
 */
import { Activity, BarChart3, Scan } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "observability.traces",
    category: "Observability",
    title: "Browse traces",
    icon: Scan,
    when: ["isAdmin"],
    navigate: "/observability/traces",
  },
  {
    id: "observability.sessions",
    category: "Observability",
    title: "Browse trace sessions",
    icon: Activity,
    when: ["isAdmin"],
    navigate: "/observability/sessions",
  },
  {
    id: "observability.analytics",
    category: "Observability",
    title: "Open analytics",
    keywords: ["cost", "tokens", "usage"],
    icon: BarChart3,
    when: ["isAdmin"],
    navigate: "/observability",
  },
]);
