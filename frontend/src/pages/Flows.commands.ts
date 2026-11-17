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
 * Agentic flow.
 *
 * Only `flows.library` carries the `flowBuilder` context key (the
 * `FLOW_BUILDER` feature flag): that tab is the way into the visual builder,
 * and it is hidden while the builder is not announced, so offering it would
 * land the reader on the Map instead. The map, its schedule view and the
 * fleets do not depend on the builder and stay listed, which is also why the
 * parent is not gated: gating it would drop its children with it.
 */
import { Workflow } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "flows.open",
    category: "Agentic Flow",
    title: "Open the agentic flow",
    icon: Workflow,
    children: [
      { id: "flows.library", category: "Agentic Flow", title: "Flow library", navigate: "/flows/library", when: ["flowBuilder"] },
      { id: "flows.map", category: "Agentic Flow", title: "Agent-to-agent map", navigate: "/flows/map" },
      { id: "flows.schedule", category: "Agentic Flow", title: "Schedule", navigate: "/flows/map/schedule" },
      { id: "flows.fleets", category: "Agentic Flow", title: "Fleets", navigate: "/fleets" },
    ],
  },
]);
