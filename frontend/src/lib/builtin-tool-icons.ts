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
 * The icons the built-in tool registry names (`icon` of
 * `GET /settings/builtin-tools/groups`), resolved to their lucide
 * component. A name the map does not know falls back to `Sparkles`.
 */
import {
  Activity, BarChart3, Blocks, Boxes, Building2, ChartLine, Clapperboard, Code2, Database, FileChartColumn, FileText,
  FlaskConical, Globe, LayoutGrid, LayoutTemplate, Library, ListTodo, Monitor, MonitorCog, Palette, Presentation, Scale,
  ScanEye, ShieldCheck, Smartphone, Sparkles, Telescope, Workflow,
  type LucideIcon,
} from "lucide-react";

const BUILTIN_TOOL_ICONS: Readonly<Record<string, LucideIcon>> = {
  Activity, BarChart3, Blocks, Boxes, Building2, ChartLine, Clapperboard, Code2, Database, FileChartColumn, FileText,
  FlaskConical, Globe, LayoutGrid, LayoutTemplate, Library, ListTodo, Monitor, MonitorCog, Palette, Presentation, Scale,
  ScanEye, ShieldCheck, Smartphone, Sparkles, Telescope, Workflow,
};

export function builtinToolIcon(name: string | undefined): LucideIcon {
  return (name && BUILTIN_TOOL_ICONS[name]) || Sparkles;
}
