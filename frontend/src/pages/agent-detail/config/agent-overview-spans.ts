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

import type { OverviewSpan } from "@/components/ui/overview-grid-spans";

/**
 * Column spans of the agent General read view (#11838), in DOM order. Every row
 * is full at every breakpoint (agent-overview-spans.test.ts):
 * - 2xl (12 columns): Name, Slug, Tags, Model, Chat, Human in the loop at 2 each,
 *   then Temperature 2, Reasoning effort 3, Organization context 4, Max
 *   iterations 3: two rows (the preloaded categories tile left with #2443). Chat and Human in the
 *   loop take 2 columns, not 1: one column is 131 px at 1920 and about 100 px at
 *   1536, which cuts "Assignment-only" and the "Human in the loop" label (measured
 *   in the lab); the tag row folds into +N, so Tags needs no third column.
 * - lg (6): 3 full rows (Name 2, Slug 1, Tags 1, Model 2 / Chat 2, Human in the
 *   loop 2, Temperature 1, Reasoning effort 1 / Organization context 4, Max
 *   iterations 2): the
 *   status tiles keep 2 columns so "Human in the loop" is not cut at 1024 (a
 *   one-column tile is 120 px there, measured in the lab).
 * - md (4): 5 rows; the two status tiles take 2 columns each so no label is cut,
 *   Max iterations alone on the last row.
 * - sm (2): 7 rows, Organization context alone on its row.
 * - base: one column.
 */
export const AGENT_OVERVIEW_SPANS = {
  name: { sm: 2, md: 2, lg: 2, "2xl": 2 },
  slug: { sm: 1, md: 2, lg: 1, "2xl": 2 },
  tags: { sm: 1, md: 2, lg: 1, "2xl": 2 },
  model: { sm: 2, md: 2, lg: 2, "2xl": 2 },
  chat: { sm: 1, md: 2, lg: 2, "2xl": 2 },
  humanInTheLoop: { sm: 1, md: 2, lg: 2, "2xl": 2 },
  temperature: { sm: 1, md: 1, lg: 1, "2xl": 2 },
  reasoningEffort: { sm: 1, md: 1, lg: 1, "2xl": 3 },
  organizationContext: { sm: 2, md: 2, lg: 4, "2xl": 4 },
  maxIterations: { sm: 2, md: 4, lg: 2, "2xl": 3 },
} as const satisfies Record<string, OverviewSpan>;

/** The DOM order of the tiles in GeneralSection's read view. */
export const AGENT_OVERVIEW_ORDER = [
  "name",
  "slug",
  "tags",
  "model",
  "chat",
  "humanInTheLoop",
  "temperature",
  "reasoningEffort",
  "organizationContext",
  "maxIterations",
] as const satisfies readonly (keyof typeof AGENT_OVERVIEW_SPANS)[];
