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
 * Pure rules of the "Outcomes" card (#9536, slice O6 of #9206; design
 * dev-docs/roi-outcomes.md 6.1): the unit picker's groups and their order,
 * the count modes a subject may use, and the draft a declaration is edited as.
 */
import type { SelectOption } from "@/components/ui/searchable-select";
import type { SegmentedFilterOption } from "@/components/ui/segmented-filter";
import type { RoiUnit } from "@/hooks/api/useRoiCatalogApi";
import type { RoiCountMode, RoiDeclaration, RoiDeclarationInput, RoiUnitSuggestion } from "@/hooks/api/useRoiDeclarationApi";

/** What accepting a suggested unit declares (#9836 R8b): one unit per finished run, editable afterwards. */
export function suggestionDeclaration(suggestion: Pick<RoiUnitSuggestion, "unit">): RoiDeclarationInput {
  return { unit: suggestion.unit, count_mode: "per_run", count_path: null, units_per_count: 1 };
}

/** i18n keys of the two group headers, shown in both modes (A2). */
const SECURITY_UNITS_GROUP = "Security units";
const CUSTOM_UNITS_GROUP = "Custom";

/**
 * The unit picker's options: archived units left out, built-ins under
 * "Security units", the organization's under "Custom". The picker runs with
 * `sort={false}` (D4), so the order is set here: XTM One mode lists the
 * security units first, Copilot mode the custom ones, and the rows of each
 * group are sorted by their label in the reader's locale.
 */
export function unitOptions(
  units: readonly RoiUnit[],
  { isXtmOne, translate, locale }: { isXtmOne: boolean; translate: (key: string) => string; locale: string },
): SelectOption[] {
  const live = units.filter((u) => !u.archived_at);
  const byLabel = (a: RoiUnit, b: RoiUnit) => a.name.localeCompare(b.name, locale, { sensitivity: "base" });
  const security = live.filter((u) => u.builtin).sort(byLabel);
  const custom = live.filter((u) => !u.builtin).sort(byLabel);
  const securityGroup = translate(SECURITY_UNITS_GROUP);
  const customGroup = translate(CUSTOM_UNITS_GROUP);
  const asOptions = (list: RoiUnit[], group: string): SelectOption[] =>
    list.map((u) => ({ value: u.key, label: u.name, description: u.role || undefined, keywords: [u.key], group }));
  const groups = isXtmOne
    ? [asOptions(security, securityGroup), asOptions(custom, customGroup)]
    : [asOptions(custom, customGroup), asOptions(security, securityGroup)];
  return groups.flat();
}

const NEEDS_JSON_SCHEMA = "Needs a JSON output schema.";

/** The three count modes; "Per output item" only on a subject with a structured output. */
export function countModeOptions(structuredOutput: boolean): SegmentedFilterOption<RoiCountMode>[] {
  return [
    { key: "per_run", label: "Per run" },
    {
      key: "per_output_item",
      label: "Per output item",
      disabled: !structuredOutput,
      disabledHint: structuredOutput ? undefined : NEEDS_JSON_SCHEMA,
    },
    { key: "per_work_item", label: "Per work item" },
  ];
}

/** What each count mode counts, one line per option under the radio group (an i18n key). @i18n-keys */
export const COUNT_MODE_MEANINGS = {
  per_run: "Per run: each finished run counts the units per run set below.",
  per_output_item: "Per output item: each item of the array at the output path counts one unit.",
  per_work_item: "Per work item: each work item the run completes counts one unit.",
} as const satisfies Record<RoiCountMode, string>;

/** Whether an agent counts structured output items: JSON output with a schema. */
export function hasStructuredOutput(agent: { output_format?: string | null; output_schema?: Record<string, unknown> | null } | undefined): boolean {
  return !!agent && agent.output_format === "json" && !!agent.output_schema && Object.keys(agent.output_schema).length > 0;
}

export interface DeclarationDraft {
  unit: string;
  count_mode: RoiCountMode;
  count_path: string;
  units_per_count: string;
}

export function draftOf(declaration: Pick<RoiDeclaration, "unit" | "count_mode" | "count_path" | "units_per_count"> | null, structuredOutput: boolean): DeclarationDraft {
  if (!declaration) return { unit: "", count_mode: "per_run", count_path: "", units_per_count: "1" };
  // A per-output-item declaration copied onto a text agent falls back to per run.
  const mode = declaration.count_mode === "per_output_item" && !structuredOutput ? "per_run" : declaration.count_mode;
  return {
    unit: declaration.unit,
    count_mode: mode,
    count_path: declaration.count_path ?? "",
    units_per_count: String(declaration.units_per_count || 1),
  };
}

export type DraftError = { field: "unit" | "count_path" | "units_per_count"; message: string };

/** The body to send, or the first field that is wrong (an i18n key). */
export function inputOf(draft: DeclarationDraft): RoiDeclarationInput | DraftError {
  if (!draft.unit) return { field: "unit", message: "Pick a unit of work." };
  if (draft.count_mode === "per_output_item" && !draft.count_path.trim()) {
    return { field: "count_path", message: "Name the output path to count, for example result.contracts." };
  }
  const units = Number(draft.units_per_count);
  if (draft.count_mode === "per_run" && (!Number.isInteger(units) || units < 1 || units > 10_000)) {
    return { field: "units_per_count", message: "Enter a whole number from 1 to 10,000." };
  }
  return {
    unit: draft.unit,
    count_mode: draft.count_mode,
    count_path: draft.count_mode === "per_output_item" ? draft.count_path.trim() : null,
    units_per_count: draft.count_mode === "per_run" ? units : 1,
  };
}

export function isDraftError(value: RoiDeclarationInput | DraftError): value is DraftError {
  return "field" in value;
}

/** What the card says when the platform refuses a save it did not catch first (i18n keys). @i18n-keys */
export const REFUSAL_MESSAGES = {
  unit: "This unit is no longer available. Pick another one.",
  count_path: "Name the output path to count, for example result.contracts.",
  units_per_count: "Enter a whole number from 1 to 10,000.",
  save: "The platform refused this declaration. Check the fields and try again.",
  reset: "The platform refused to remove this declaration. Try again.",
} as const satisfies Record<DraftError["field"] | "save" | "reset", string>;

/**
 * A refused save (UX round 4, B1): the field the 422 names and its translated
 * message, or the generic sentence. The backend text (English, with the
 * internal unit key) is never shown as is.
 */
export function saveRefusalOf(err: unknown): { field: DraftError["field"] | null; message: string } {
  const { status, detail } = (err ?? {}) as { status?: number; detail?: unknown };
  if (status === 422 && typeof detail === "string") {
    if (/^unit .* is not a known unit$/.test(detail)) return { field: "unit", message: REFUSAL_MESSAGES.unit };
    if (detail.startsWith("count_path ")) return { field: "count_path", message: REFUSAL_MESSAGES.count_path };
    if (detail.startsWith("units_per_count ")) {
      return { field: "units_per_count", message: REFUSAL_MESSAGES.units_per_count };
    }
  }
  return { field: null, message: REFUSAL_MESSAGES.save };
}
