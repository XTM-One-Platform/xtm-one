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
 * Flow builder canvas commands.
 *
 * Every entry is also a toolbar button and a keyboard shortcut; the palette is
 * the discoverable third path for anyone who has not learnt either yet. The
 * behaviour is registered at runtime by the builder screen
 * (`useCommandHandler`), so listing a command never drags the canvas chunk into
 * the entry bundle.
 *
 * All of them are behind the `flowBuilder` context key (the `FLOW_BUILDER`
 * feature flag), like the `/flows` pages they act on: the palette lists
 * commands from screens that are not mounted, so without the key these would be
 * offered on an instance where the builder is hidden.
 */
import {
  Download,
  History,
  LayoutGrid,
  ListChecks,
  Play,
  Power,
  Save,
} from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "flowBuilder.save",
    category: "Flow builder",
    title: "Save the flow",
    keywords: ["commit", "version"],
    icon: Save,
    handler: "flowBuilder.save",
    when: ["flowBuilder"],
  },
  {
    id: "flowBuilder.autoLayout",
    category: "Flow builder",
    title: "Tidy up the layout",
    keywords: ["arrange", "align", "dagre"],
    icon: LayoutGrid,
    handler: "flowBuilder.autoLayout",
    when: ["flowBuilder"],
  },
  {
    id: "flowBuilder.export",
    category: "Flow builder",
    title: "Export the flow as JSON",
    keywords: ["download", "backup"],
    icon: Download,
    handler: "flowBuilder.export",
    when: ["flowBuilder"],
  },
  {
    id: "flowBuilder.versions",
    category: "Flow builder",
    title: "Show the version history",
    keywords: ["restore", "revert", "history"],
    icon: History,
    handler: "flowBuilder.versions",
    when: ["flowBuilder"],
  },
  {
    id: "flowBuilder.run",
    category: "Flow builder",
    title: "Run the flow",
    keywords: ["execute", "test", "trigger"],
    icon: Play,
    handler: "flowBuilder.run",
    when: ["flowBuilder"],
  },
  {
    id: "flowBuilder.toggleActive",
    category: "Flow builder",
    title: "Activate or deactivate the flow",
    keywords: ["enable", "disable", "live", "triggers"],
    icon: Power,
    handler: "flowBuilder.toggleActive",
    when: ["flowBuilder"],
  },
  {
    id: "flowBuilder.runs",
    category: "Flow builder",
    title: "Show the run history",
    keywords: ["executions", "trace", "logs"],
    icon: ListChecks,
    handler: "flowBuilder.runs",
    when: ["flowBuilder"],
  },
]);
