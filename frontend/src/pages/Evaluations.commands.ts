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
 * Evaluations: offered only while evaluation is on, never in XTM One mode.
 */
import { Database, FlaskConical, Plus } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "evaluations.create",
    category: "Evaluations",
    title: "Create an evaluation",
    icon: Plus,
    when: ["isAdmin", "evaluation"],
    navigate: "/evaluations/new",
  },
  {
    id: "evaluations.datasets",
    category: "Evaluations",
    title: "Browse datasets",
    icon: Database,
    when: ["isAdmin", "evaluation"],
    navigate: "/evaluations/datasets",
  },
  {
    id: "evaluations.list",
    category: "Evaluations",
    title: "Browse evaluations",
    icon: FlaskConical,
    when: ["isAdmin", "evaluation"],
    navigate: "/evaluations",
  },
]);
