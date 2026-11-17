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
 * The icon of each goal-plan action (`GOAL_ACTION_ICONS` server-side), shared
 * by the block that draws a run's plan and the pack builder that edits one.
 */
import {
  BookOpen,
  Database,
  FileText,
  Fingerprint,
  GitBranch,
  Globe,
  Network,
  ScanSearch,
  Search,
  Target,
} from "lucide-react";

export const ACTION_ICONS: Record<string, typeof Target> = {
  target: Target,
  fingerprint: Fingerprint,
  network: Network,
  branch: GitBranch,
  scan: ScanSearch,
  book: BookOpen,
  search: Search,
  globe: Globe,
  file: FileText,
  database: Database,
};
