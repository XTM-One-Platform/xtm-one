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
 * The one set of words for what a connected member receives
 * (`context_inclusion`, shown as "Conversation shared"): the members panel,
 * the Members view, the "New fleet" dialog and the agent page's Fleet card
 * read this map, so they never drift (dev-docs/ctem-fleets.md 10.2).
 */
import type { FleetContextInclusion } from "@/hooks/api/useFleetApi";

/** The name of the setting, the label of its pickers and of its column. @i18n-keys */
const FIELD_LABELS = { context_inclusion: "Conversation shared" } as const;
export const CONTEXT_INCLUSION_FIELD = FIELD_LABELS.context_inclusion;

/** The English label of each context inclusion in a picker, read through `t_i18n`. @i18n-keys */
export const CONTEXT_INCLUSION_LABELS = {
  none: "Nothing",
  summary: "A summary",
  full: "The full conversation",
} as const satisfies Record<FleetContextInclusion, string>;

/** The short label of each value, in a table cell. "Full" alone is never shown. @i18n-keys */
export const CONTEXT_INCLUSION_SHORT_LABELS = {
  none: "Nothing",
  summary: "Summary",
  full: "Full conversation",
} as const satisfies Record<FleetContextInclusion, string>;

/** One line per value: what the connected agent receives with its task, shown under the option in a picker. @i18n-keys */
const CONTEXT_INCLUSION_DESCRIPTIONS = {
  none: "The agent receives only the task it is given, no part of the conversation.",
  summary: "The agent receives a short summary of the conversation with the task.",
  full: "The agent receives the whole conversation with the task.",
} as const satisfies Record<FleetContextInclusion, string>;

/** The options of a context picker, in display order. */
export const CONTEXT_INCLUSIONS: { value: FleetContextInclusion; label: string; description: string }[] = (
  ["none", "summary", "full"] as const
).map((value) => ({ value, label: CONTEXT_INCLUSION_LABELS[value], description: CONTEXT_INCLUSION_DESCRIPTIONS[value] }));
